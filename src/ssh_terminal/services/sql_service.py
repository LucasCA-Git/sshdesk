"""SQL console over an SSH tunnel (MySQL/MariaDB and PostgreSQL).

The database never has to be exposed: a local port forward (``direct-tcpip``,
like ``ssh -L``) is opened through the existing SSH connection to
``<host>:<port>`` *as seen from the server* (usually ``127.0.0.1:3306/5432``),
and the driver connects to that ephemeral local port.

Drivers are optional and imported lazily: PyMySQL (pure Python) for MySQL and
MariaDB, psycopg 3 for PostgreSQL.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ssh_terminal.models.connection import PortForwardSpec
from ssh_terminal.ssh.forwarding import LocalForwarder

KINDS = {"mysql": ("MySQL / MariaDB", 3306), "postgres": ("PostgreSQL", 5432)}
DEFAULT_MAX_ROWS = 5000
HISTORY_LIMIT = 300
HISTORY_SQL_MAX = 4000  # characters kept per statement
_SECRETS = re.compile(r"((?:identified\s+(?:with\s+\S+\s+)?by|password\s*(?:=|\(|for\s+\S+\s*=)?)\s*)"
                      r"('(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"|\$\$.*?\$\$)", re.IGNORECASE | re.DOTALL)


def redact(sql: str) -> str:
    """History never keeps passwords: ``IDENTIFIED BY '…'`` / ``PASSWORD '…'`` become ``'***'``."""
    sql = _SECRETS.sub(lambda m: m.group(1) + "'***'", sql)
    return sql if len(sql) <= HISTORY_SQL_MAX else sql[:HISTORY_SQL_MAX] + " …"


class SqlError(Exception):
    """A database or driver error, with a readable message (and position when known)."""

    def __init__(self, message: str, position: int | None = None) -> None:
        super().__init__(message)
        self.position = position


class DriverMissing(SqlError):
    pass


class QueryCancelled(SqlError):
    pass


# ---------------------------------------------------------------------- #
# Profiles (no passwords: those go to the keyring / encrypted vault)
# ---------------------------------------------------------------------- #
@dataclass
class SqlProfile:
    kind: str = "mysql"
    host: str = "127.0.0.1"  # as seen from the SSH server
    port: int = 3306
    user: str = ""
    database: str = ""
    read_only: bool = False
    remember: bool = False

    def key(self, alias: str) -> str:
        return f"sql:{alias}:{self.kind}:{self.user}@{self.host}:{self.port}"

    def label(self) -> str:
        return f"{KINDS.get(self.kind, (self.kind,))[0]} · {self.user}@{self.host}:{self.port}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SqlProfile:
        p = cls()
        for name in ("kind", "host", "user", "database"):
            if isinstance(data.get(name), str):
                setattr(p, name, data[name])
        for name in ("read_only", "remember"):
            if isinstance(data.get(name), bool):
                setattr(p, name, data[name])
        if isinstance(data.get("port"), int) and 0 < data["port"] < 65536:
            p.port = data["port"]
        if p.kind not in KINDS:
            p.kind = "mysql"
        return p


class JsonStore:
    """Small JSON file in the app directory (profiles, history)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # history can hold table names and data from WHERE clauses: owner only
        except OSError:
            pass
        tmp.replace(self.path)


class ProfileStore(JsonStore):
    def profiles(self, alias: str) -> list[SqlProfile]:
        return [SqlProfile.from_dict(d) for d in self.load().get(alias, []) if isinstance(d, dict)]

    def remember(self, alias: str, profile: SqlProfile) -> None:
        data = self.load()
        items = [SqlProfile.from_dict(d) for d in data.get(alias, []) if isinstance(d, dict)]
        items = [p for p in items if p.key(alias) != profile.key(alias)]
        items.insert(0, profile)
        data[alias] = [asdict(p) for p in items[:10]]
        self.save(data)

    def forget(self, alias: str, profile: SqlProfile) -> None:
        data = self.load()
        data[alias] = [d for d in data.get(alias, []) if SqlProfile.from_dict(d).key(alias) != profile.key(alias)]
        self.save(data)


@dataclass
class HistoryEntry:
    sql: str
    database: str
    when: str
    ms: int
    status: str  # "ok" | "error" | "cancelled"
    rows: int = 0


class HistoryStore(JsonStore):
    def entries(self, server: str) -> list[HistoryEntry]:
        raw = self.load().get(server, [])
        out = []
        for d in raw if isinstance(raw, list) else []:
            try:
                out.append(HistoryEntry(**{k: d[k] for k in ("sql", "database", "when", "ms", "status")}, rows=int(d.get("rows", 0))))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def add(self, server: str, entry: HistoryEntry) -> None:
        data = self.load()
        items = [d for d in data.get(server, []) if isinstance(d, dict)]
        entry.sql = redact(entry.sql)
        items.insert(0, asdict(entry))
        data[server] = items[:HISTORY_LIMIT]
        self.save(data)


# ---------------------------------------------------------------------- #
# SQL text helpers (no driver needed)
# ---------------------------------------------------------------------- #
CODE, STRING, COMMENT = "code", "string", "comment"


def _scan(sql: str, dialect: str = "mysql"):  # noqa: ANN202 - yields (index, char, kind)
    """Walk ``sql`` reporting for each char whether it is code, inside a string/identifier, or a comment."""
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        two = sql[i:i + 2]
        if two == "--" or (c == "#" and dialect == "mysql"):  # in PostgreSQL # is an operator
            end = sql.find("\n", i)
            end = n if end < 0 else end
            for j in range(i, end):
                yield j, sql[j], COMMENT
            i = end
            continue
        if two == "/*":
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for j in range(i, end):
                yield j, sql[j], COMMENT
            i = end
            continue
        if c in "'\"" or (c == "`" and dialect == "mysql"):
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c != "`" and dialect == "mysql":  # PostgreSQL: backslash is literal
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:  # doubled quote
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            for k in range(i, end):
                yield k, sql[k], STRING
            i = end
            continue
        m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:]) if c == "$" and dialect == "postgres" else None
        if m:  # PostgreSQL dollar quoting: $$ ... $$ / $tag$ ... $tag$
            tag = m.group(0)
            close = sql.find(tag, i + len(tag))
            end = n if close < 0 else close + len(tag)
            for k in range(i, end):
                yield k, sql[k], STRING
            i = end
            continue
        yield i, c, CODE
        i += 1


def split_statements(sql: str, dialect: str = "mysql") -> list[tuple[int, int, str]]:
    """``(start, end, text)`` of each statement separated by ``;`` (quotes and comments respected)."""
    out, start = [], 0
    for i, c, kind in _scan(sql, dialect):
        if kind == CODE and c == ";":
            out.append((start, i + 1, sql[start:i + 1]))
            start = i + 1
    if sql[start:].strip():
        out.append((start, len(sql), sql[start:]))
    return [(s, e, t) for s, e, t in out if strip_comments(t, dialect).strip().rstrip(";").strip()]


def statement_at(sql: str, pos: int, dialect: str = "mysql") -> str:
    """The statement under the cursor (or the last one before it)."""
    statements = split_statements(sql, dialect)
    for s, e, text in statements:
        if s <= pos <= e:
            return text.strip()
    before = [t for s, _e, t in statements if s <= pos]
    return (before[-1] if before else (statements[0][2] if statements else "")).strip()


def strip_comments(sql: str, dialect: str = "mysql") -> str:
    """The SQL without comments (strings kept)."""
    return "".join(c for _i, c, kind in _scan(sql, dialect) if kind != COMMENT)


def code_only(sql: str, dialect: str = "mysql") -> str:
    """The SQL with strings and comments blanked out (for keyword checks)."""
    return "".join(c if kind == CODE else " " for _i, c, kind in _scan(sql, dialect))


_READ_KEYWORDS = {"select", "show", "describe", "desc", "explain", "values", "table", "with", "use", "help"}
_WRITE_WORDS = re.compile(r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|call|do|copy|"
                          r"lock|into|outfile|dumpfile|analyze|load_file)\b", re.IGNORECASE)
# SET is allowed only for harmless session settings; never for transaction mode, passwords, roles or globals
_SAFE_SET = re.compile(r"^\s*set\s+(?:session\s+|local\s+)?(names|character\s+set|charset|time\s+zone|time_zone|"
                       r"timezone|search_path|datestyle|client_encoding|@[a-z_][a-z0-9_]*)\b", re.IGNORECASE)
_UNSAFE_SET = re.compile(r"\b(read|write|transaction|password|role|global|persist|authorization|default_transaction|"
                         r"session_authorization)\b", re.IGNORECASE)


def first_keyword(sql: str, dialect: str = "mysql") -> str:
    m = re.match(r"\s*\(*\s*([A-Za-z]+)", code_only(sql, dialect))
    return m.group(1).lower() if m else ""


def is_read_only_statement(sql: str, dialect: str = "mysql") -> bool:
    """Allowed on a read-only connection (an allow-list; the server is also set to read-only)."""
    code = code_only(sql, dialect)
    keyword = first_keyword(sql, dialect)
    if keyword == "set":
        return bool(_SAFE_SET.match(code)) and not _UNSAFE_SET.search(code)
    if keyword not in _READ_KEYWORDS:
        return False
    # SELECT ... INTO (OUTFILE / a new table), EXPLAIN ANALYZE (executes), writes inside WITH
    return not _WRITE_WORDS.search(code)


def destructive_reason(sql: str, dialect: str = "mysql") -> str | None:
    """Why a statement deserves a confirmation (None when it is safe enough)."""
    code = code_only(sql, dialect)
    keyword = first_keyword(sql, dialect)
    if keyword == "drop":
        return "DROP removes the object and all its data."
    if keyword == "truncate":
        return "TRUNCATE deletes every row of the table."
    if keyword in ("delete", "update") and not re.search(r"\bwhere\b", code, re.IGNORECASE):
        return f"{keyword.upper()} without WHERE changes every row of the table."
    if keyword == "alter" and re.search(r"\bdrop\b", code, re.IGNORECASE):
        return "ALTER ... DROP removes a column, index or constraint."
    return None


def quote_ident(kind: str, name: str) -> str:
    if kind == "postgres":
        return ".".join('"' + part.replace('"', '""') + '"' for part in name.split(".", 1)) if "." in name \
            else '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


# ---------------------------------------------------------------------- #
# Tunnel
# ---------------------------------------------------------------------- #
class SqlTunnel:
    """``127.0.0.1:<random>`` here → ``host:port`` as seen from the SSH server."""

    def __init__(self, connection, host: str, port: int) -> None:  # noqa: ANN001 - SSHConnection
        spec = PortForwardSpec("local", "127.0.0.1", 0, host, port)
        self.forwarder = LocalForwarder(spec, connection.transport)
        self.local_port = spec.bind_port

    @property
    def active(self) -> bool:
        return self.forwarder.active

    def close(self) -> None:
        self.forwarder.stop()


# ---------------------------------------------------------------------- #
# Sessions
# ---------------------------------------------------------------------- #
@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    key: str = ""
    default: str | None = None


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    affected: int | None = None  # statements without a result set
    elapsed_ms: int = 0
    truncated: bool = False
    message: str = ""

    @property
    def has_rows(self) -> bool:
        return bool(self.columns)


class SqlSession:
    """Common interface of the MySQL and PostgreSQL sessions."""

    kind = ""
    version = ""

    def __init__(self, profile: SqlProfile, password: str, local_port: int) -> None:
        self.profile = profile
        self.password = password
        self.local_port = local_port
        self.database = profile.database

    # implemented by subclasses
    def databases(self) -> list[str]: raise NotImplementedError  # noqa: E704
    def tables(self) -> list[str]: raise NotImplementedError  # noqa: E704
    def columns(self, table: str) -> list[Column]: raise NotImplementedError  # noqa: E704
    def indexes(self, table: str) -> list[tuple[str, str, bool]]: raise NotImplementedError  # noqa: E704
    def use(self, database: str) -> None: raise NotImplementedError  # noqa: E704
    def cancel(self) -> None: raise NotImplementedError  # noqa: E704
    def close(self) -> None: raise NotImplementedError  # noqa: E704
    def _run(self, sql: str, max_rows: int) -> QueryResult: raise NotImplementedError  # noqa: E704
    def set_timeout(self, seconds: int) -> None: raise NotImplementedError  # noqa: E704

    def execute(self, sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> QueryResult:
        if self.profile.read_only and not all(is_read_only_statement(t, self.kind)
                                              for _s, _e, t in split_statements(sql, self.kind)):
            raise SqlError("This connection is read-only: only SELECT / SHOW / EXPLAIN-like statements are allowed.")
        started = time.monotonic()
        result = self._run(sql, max(1, int(max_rows)))
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def qualified(self, table: str) -> str:
        """The table as SQL (quoted); ``table`` is a name as listed by :meth:`tables`."""
        return quote_ident(self.kind, table)

    def count(self, table: str) -> int:
        result = self._run(f"SELECT COUNT(*) FROM {self.qualified(table)}", 1)
        return int(result.rows[0][0]) if result.rows else 0


def _load(module: str, package: str):  # noqa: ANN202
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise DriverMissing(f"The {package} driver is not installed (pip install {package}).") from exc


class MySqlSession(SqlSession):
    kind = "mysql"

    def __init__(self, profile: SqlProfile, password: str, local_port: int, timeout: int = 10) -> None:
        super().__init__(profile, password, local_port)
        self._pymysql = _load("pymysql", "PyMySQL")
        self._timeout = timeout
        self.conn = self._connect(profile.database or None)
        self.thread_id = self.conn.thread_id()
        raw = self._scalar("SELECT VERSION()") or ""
        self.version = ("MariaDB " if "mariadb" in raw.lower() else "MySQL ") + raw.split("-")[0]
        if profile.read_only:
            self._exec("SET SESSION TRANSACTION READ ONLY")
        if not self.database:
            self.database = self._scalar("SELECT DATABASE()") or ""

    def _connect(self, database: str | None):  # noqa: ANN202
        try:
            return self._pymysql.connect(
                host="127.0.0.1", port=self.local_port, user=self.profile.user, password=self.password,
                database=database, connect_timeout=self._timeout, charset="utf8mb4", autocommit=True,
            )
        except self._pymysql.MySQLError as exc:
            raise SqlError(_mysql_message(exc)) from exc

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, args or None)
                return list(cur.fetchall())
        except self._pymysql.MySQLError as exc:
            raise SqlError(_mysql_message(exc)) from exc

    def _scalar(self, sql: str) -> Any:  # noqa: ANN401
        rows = self._exec(sql)
        return rows[0][0] if rows else None

    def databases(self) -> list[str]:
        return [r[0] for r in self._exec("SHOW DATABASES")]

    def tables(self) -> list[str]:
        if not self.database:
            return []
        return [r[0] for r in self._exec(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
            (self.database,))]

    def columns(self, table: str) -> list[Column]:
        rows = self._exec(
            "SELECT column_name, column_type, is_nullable, column_key, column_default FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (self.database, table))
        return [Column(r[0], r[1], r[2] == "YES", r[3] or "", None if r[4] is None else str(r[4])) for r in rows]

    def indexes(self, table: str) -> list[tuple[str, str, bool]]:
        rows = self._exec(
            "SELECT index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index), MIN(non_unique) = 0 "
            "FROM information_schema.statistics WHERE table_schema = %s AND table_name = %s GROUP BY index_name",
            (self.database, table))
        return [(r[0], r[1], bool(r[2])) for r in rows]

    def use(self, database: str) -> None:
        self._exec(f"USE {quote_ident('mysql', database)}")
        self.database = database

    def set_timeout(self, seconds: int) -> None:
        """Server-side limit: MariaDB ``max_statement_time`` (s), MySQL ``max_execution_time`` (ms, SELECT only)."""
        seconds = max(0, int(seconds))
        if seconds == getattr(self, "_timeout_set", 0):
            return
        try:
            if self.version.startswith("MariaDB"):
                self._exec(f"SET SESSION max_statement_time = {seconds}")
            else:
                self._exec(f"SET SESSION max_execution_time = {seconds * 1000}")
            self._timeout_set = seconds
        except SqlError:
            pass  # old server without the variable: no limit

    def _run(self, sql: str, max_rows: int) -> QueryResult:
        cursors = self._pymysql.cursors
        cur = self.conn.cursor(cursors.SSCursor)  # unbuffered: stop reading at max_rows
        truncated = False
        try:
            cur.execute(sql)
            if cur.description is None:
                result = QueryResult(affected=cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)
            else:
                columns = [d[0] for d in cur.description]
                rows = list(cur.fetchmany(max_rows + 1))
                truncated = len(rows) > max_rows
                result = QueryResult(columns, rows[:max_rows], truncated=truncated)
            if first_keyword(sql, "mysql") == "use":
                self.database = self._scalar_on(cur) or self.database
        except self._pymysql.MySQLError as exc:
            code = exc.args[0] if exc.args else 0
            if code in (1317, 1969, 3024):  # interrupted / MariaDB max_statement_time / MySQL max_execution_time
                raise QueryCancelled("Query cancelled.") from exc
            raise SqlError(_mysql_message(exc)) from exc
        finally:
            if truncated:
                self.cancel()  # don't stream the rest of a huge result
            try:
                cur.close()
            except self._pymysql.MySQLError:
                pass
        return result

    def _scalar_on(self, _cur) -> str | None:  # noqa: ANN001
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT DATABASE()")
                row = cur.fetchone()
                return row[0] if row else None
        except self._pymysql.MySQLError:
            return None

    def cancel(self) -> None:
        """``KILL QUERY`` from a second connection (the first one is busy)."""
        try:
            killer = self._connect(None)
        except SqlError:
            return
        try:
            with killer.cursor() as cur:
                cur.execute(f"KILL QUERY {int(self.thread_id)}")
        except self._pymysql.MySQLError:
            pass
        finally:
            killer.close()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def _mysql_message(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 2:
        return f"{args[1]} (error {args[0]})"
    return str(exc)


class PostgresSession(SqlSession):
    kind = "postgres"

    def __init__(self, profile: SqlProfile, password: str, local_port: int, timeout: int = 10) -> None:
        super().__init__(profile, password, local_port)
        self._psycopg = _load("psycopg", "psycopg[binary]")
        self._timeout = timeout
        self.conn = self._connect(profile.database or "postgres")
        self.version = "PostgreSQL " + (self._scalar("SHOW server_version") or "").split()[0]
        self.database = self._scalar("SELECT current_database()") or self.database

    def _connect(self, database: str):  # noqa: ANN202
        try:
            conn = self._psycopg.connect(
                host="127.0.0.1", port=self.local_port, user=self.profile.user, password=self.password,
                dbname=database, connect_timeout=self._timeout, autocommit=True, application_name="VeyLix",
            )
        except self._psycopg.Error as exc:
            raise SqlError(_pg_message(exc)) from exc
        if self.profile.read_only:
            conn.execute("SET default_transaction_read_only = on")
        return conn

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, args or None)
                return list(cur.fetchall()) if cur.description else []
        except self._psycopg.Error as exc:
            raise SqlError(_pg_message(exc)) from exc

    def _scalar(self, sql: str) -> Any:  # noqa: ANN401
        rows = self._exec(sql)
        return rows[0][0] if rows else None

    def databases(self) -> list[str]:
        return [r[0] for r in self._exec("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname")]

    def tables(self) -> list[str]:
        rows = self._exec(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY table_schema <> 'public', 1, 2")
        self._tables = {(name if schema == "public" else f"{schema}.{name}"): (schema, name) for schema, name in rows}
        return list(self._tables)

    def _split(self, table: str) -> tuple[str, str]:
        known = getattr(self, "_tables", {}).get(table)
        if known is not None:  # exact (schema, name), even when the name itself contains a dot
            return known
        return tuple(table.split(".", 1)) if "." in table else ("public", table)  # type: ignore[return-value]

    def qualified(self, table: str) -> str:
        schema, name = self._split(table)
        quote = lambda part: '"' + part.replace('"', '""') + '"'  # noqa: E731
        return f"{quote(schema)}.{quote(name)}"

    def columns(self, table: str) -> list[Column]:
        schema, name = self._split(table)
        rows = self._exec(
            "SELECT c.column_name, c.data_type, c.is_nullable, "
            "CASE WHEN EXISTS (SELECT 1 FROM information_schema.key_column_usage k "
            "  JOIN information_schema.table_constraints t ON t.constraint_name = k.constraint_name "
            "  AND t.table_schema = k.table_schema "
            "  WHERE t.constraint_type = 'PRIMARY KEY' AND k.table_schema = c.table_schema "
            "  AND k.table_name = c.table_name AND k.column_name = c.column_name) THEN 'PRI' ELSE '' END, "
            "c.column_default FROM information_schema.columns c "
            "WHERE c.table_schema = %s AND c.table_name = %s ORDER BY c.ordinal_position", (schema, name))
        return [Column(r[0], r[1], r[2] == "YES", r[3], r[4]) for r in rows]

    def indexes(self, table: str) -> list[tuple[str, str, bool]]:
        schema, name = self._split(table)
        rows = self._exec("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
                          (schema, name))
        return [(r[0], r[1], "UNIQUE" in r[1].upper()) for r in rows]

    def use(self, database: str) -> None:
        """PostgreSQL has no USE: reconnect to the other database through the same tunnel."""
        new = self._connect(database)
        self.close()
        self.conn = new
        self.database = database
        self._timeout_set = 0

    def set_timeout(self, seconds: int) -> None:
        seconds = max(0, int(seconds))
        if seconds == getattr(self, "_timeout_set", 0):
            return
        self._exec(f"SET statement_timeout = {seconds * 1000}")
        self._timeout_set = seconds

    def _streamable(self, sql: str) -> bool:
        statements = split_statements(sql, "postgres")
        return (len(statements) == 1 and first_keyword(sql, "postgres") in ("select", "with", "values", "table")
                and not _WRITE_WORDS.search(code_only(sql, "postgres")))

    def _run(self, sql: str, max_rows: int) -> QueryResult:
        if self._streamable(sql):  # DECLARE a cursor and read only max_rows + 1 rows from it
            try:
                with self.conn.transaction(), self.conn.cursor(name="veylix_result") as cur:
                    cur.execute(sql.strip().rstrip(";"))
                    rows = cur.fetchmany(max_rows + 1)
                    columns = [d.name for d in cur.description or []]
                return QueryResult(columns, [tuple(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows)
            except self._psycopg.errors.QueryCanceled as exc:
                raise QueryCancelled("Query cancelled.") from exc
            except self._psycopg.Error as exc:
                raise SqlError(_pg_message(exc), _pg_position(exc)) from exc
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    return QueryResult(affected=cur.rowcount if cur.rowcount >= 0 else 0,
                                       message=cur.statusmessage or "")
                columns = [d.name for d in cur.description]
                rows = cur.fetchmany(max_rows + 1)
                return QueryResult(columns, [tuple(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows)
        except self._psycopg.errors.QueryCanceled as exc:
            raise QueryCancelled("Query cancelled.") from exc
        except self._psycopg.Error as exc:
            raise SqlError(_pg_message(exc), _pg_position(exc)) from exc

    def cancel(self) -> None:
        try:
            self.conn.cancel_safe() if hasattr(self.conn, "cancel_safe") else self.conn.cancel()
        except self._psycopg.Error:
            pass

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def _pg_message(exc: Exception) -> str:
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    return primary or str(exc).strip()


def _pg_position(exc: Exception) -> int | None:
    diag = getattr(exc, "diag", None)
    pos = getattr(diag, "statement_position", None) if diag is not None else None
    try:
        return int(pos) if pos else None
    except ValueError:
        return None


def open_session(profile: SqlProfile, password: str, local_port: int) -> SqlSession:
    if profile.kind == "postgres":
        return PostgresSession(profile, password, local_port)
    return MySqlSession(profile, password, local_port)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
