"""Docker / SQL service logic that needs no server: parsing, escaping, classification, SQL text helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ssh_terminal.services.docker_service import (
    Container,
    Problem,
    action_command,
    allowed_actions,
    classify,
    database_candidates,
    logs_command,
    parse_containers,
    parse_images,
    safe_ref,
    state_from_status,
)
from ssh_terminal.services.remote_exec import CommandResult, RemoteRunner
from ssh_terminal.services.sql_service import (
    HistoryEntry,
    HistoryStore,
    ProfileStore,
    SqlProfile,
    code_only,
    destructive_reason,
    is_read_only_statement,
    quote_ident,
    split_statements,
    statement_at,
)

PS = "\n".join([
    json.dumps({"ID": "426f4abd0843a307a377", "Names": "web", "Image": "nginx:1.27", "Status": "Up 3 days",
                "State": "running", "Ports": "0.0.0.0:8081->80/tcp", "CreatedAt": "2026-10-10"}),
    json.dumps({"ID": "306805736db2", "Names": "cache", "Image": "redis", "Status": "Up 2 hours (Paused)", "Ports": ""}),
    "WARNING: some plugin printed this",
    json.dumps({"ID": "d3f5f336ad93", "Names": "job,alias", "Image": "busybox", "Status": "Exited (0) 3 hours ago"}),
    "",
])


def test_parse_docker_ps_json_lines() -> None:
    items = parse_containers(PS)
    assert [c.name for c in items] == ["web", "cache", "job"]  # junk lines skipped, first name kept
    assert [c.state for c in items] == ["running", "paused", "exited"]  # State or derived from Status
    assert items[0].short_id == "426f4abd0843" and items[0].running and items[0].ports == "0.0.0.0:8081->80/tcp"


def test_state_from_status_for_old_docker() -> None:
    assert state_from_status("Up 5 minutes (Paused)") == "paused"
    assert state_from_status("Restarting (1) 2 seconds ago") == "restarting"
    assert state_from_status("Created") == "created"
    assert state_from_status("Exited (137) 1 day ago") == "exited"


def test_parse_docker_images() -> None:
    text = "\n".join([
        json.dumps({"Repository": "nginx", "Tag": "1.27", "ID": "abc123", "Size": "187MB", "CreatedSince": "2 weeks ago"}),
        json.dumps({"Repository": "<none>", "Tag": "<none>", "ID": "def456", "Size": "12MB", "CreatedAt": "x"}),
    ])
    images = parse_images(text)
    assert images[0].ref == "nginx:1.27" and images[0].created == "2 weeks ago"
    assert images[1].ref == "def456"  # dangling images are removed by ID


@pytest.mark.parametrize("bad", ["web; rm -rf /", "$(reboot)", "-rf", "web name", "a`b`", "../x", "", "a|b", "x\ny"])
def test_unsafe_refs_never_reach_the_shell(bad: str) -> None:
    with pytest.raises(ValueError):
        safe_ref(bad)
    with pytest.raises(ValueError):
        action_command("stop", bad)


def test_commands_are_built_and_quoted() -> None:
    assert safe_ref("registry.example.com:5000/team/app:1.2@sha256:abc") == "registry.example.com:5000/team/app:1.2@sha256:abc"
    assert action_command("restart", "web") == "docker restart web"
    assert action_command("remove", "web", force=True, volumes=True) == "docker rm -f -v web"
    with pytest.raises(ValueError):
        action_command("exec", "web")
    assert logs_command("web", 300, follow=True, timestamps=True) == "docker logs --tail 300 -f -t web"
    assert logs_command("web", 10**9) == "docker logs --tail 100000 web"  # capped


def test_allowed_actions_follow_the_state() -> None:
    assert "start" not in allowed_actions("running") and "pause" in allowed_actions("running")
    assert allowed_actions("paused") == {"unpause", "stop", "remove"}
    assert "start" in allowed_actions("exited") and "pause" not in allowed_actions("exited")


@pytest.mark.parametrize("status,err,problem", [
    (127, "sh: 1: docker: not found", Problem.NOT_INSTALLED),
    (1, "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock",
     Problem.PERMISSION),
    (1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
     Problem.DAEMON_DOWN),
    (1, "failed to connect to the docker API at unix:///var/run/docker.sock; check if the daemon is running",
     Problem.DAEMON_DOWN),
    (1, "sudo: 1 incorrect password attempt", Problem.SUDO_PASSWORD),
    (1, "Error response from daemon: No such container: x", Problem.OTHER),
])
def test_classify_docker_errors(status: int, err: str, problem: Problem) -> None:
    assert classify(CommandResult(status, "", err)) is problem


def test_database_candidates_from_published_ports() -> None:
    containers = [
        Container("1", "pg", "postgres:16-alpine", "Up", "running", "0.0.0.0:5433->5432/tcp, :::5433->5432/tcp"),
        Container("2", "maria", "bitnami/mariadb:11", "Up", "running", "127.0.0.1:3307->3306/tcp"),
        Container("3", "internal", "mysql:8", "Up", "running", "3306/tcp"),  # not published
        Container("4", "old", "postgres:15", "Exited", "exited", "0.0.0.0:5434->5432/tcp"),  # stopped
    ]
    assert [(c.container, c.kind, c.port) for c in database_candidates(containers)] == [("pg", "postgres", 5433),
                                                                                          ("maria", "mysql", 3307)]


def test_sudo_wrapping_quotes_the_whole_command() -> None:
    runner = RemoteRunner(connection=None)
    assert runner.wrap("docker ps") == "docker ps"
    runner.enable_sudo("s3cret")
    assert runner.wrap("docker logs 'a b'") == "sudo -S -p '' sh -c 'docker logs '\"'\"'a b'\"'\"''"
    assert "s3cret" not in runner.wrap("docker ps")  # the password goes on stdin, never in the command
    runner.enable_sudo(None)
    assert runner.wrap("docker ps").startswith("sudo -n sh -c ")
    runner.disable_sudo()
    assert runner.wrap("docker ps") == "docker ps"


# ---------------------------------------------------------------------- #
def test_split_statements_respects_strings_comments_and_dollar_quotes() -> None:
    sql = "SELECT 1; -- a;b\nSELECT ';' x; /* ; */ DELETE FROM t;\n\nSELECT $$a;b$$; SELECT `a;b` FROM t"
    my = [t.strip() for _s, _e, t in split_statements(sql)]
    assert my[0] == "SELECT 1;" and my[1].endswith("SELECT ';' x;") and my[2].endswith("DELETE FROM t;")
    assert my[-1] == "SELECT `a;b` FROM t"
    pg = [t.strip() for _s, _e, t in split_statements(sql, "postgres")]
    assert "SELECT $$a;b$$;" in pg
    assert split_statements("SELECT data #> '{a}' FROM t; SELECT 2", "postgres")[0][2].startswith("SELECT data #>")
    assert split_statements("  ;; -- only comments\n") == []


def test_statement_under_the_cursor() -> None:
    sql = "SELECT 1;\nSELECT 2;\n\nUPDATE t SET a = 1 WHERE id = 3"
    assert statement_at(sql, 0) == "SELECT 1;"
    assert statement_at(sql, sql.index("2")) == "SELECT 2;"
    assert statement_at(sql, len(sql)) == "UPDATE t SET a = 1 WHERE id = 3"


@pytest.mark.parametrize("sql,danger", [
    ("DROP TABLE users", True),
    ("truncate orders", True),
    ("DELETE FROM users", True),
    ("delete from users -- where id = 1", True),
    ("DELETE FROM users WHERE id = 1", False),
    ("UPDATE users SET note = 'where'", True),
    ("UPDATE users SET a = 1 WHERE id = 2", False),
    ("ALTER TABLE t DROP COLUMN c", True),
    ("ALTER TABLE t ADD COLUMN c int", False),
    ("SELECT * FROM t", False),
])
def test_destructive_statements(sql: str, danger: bool) -> None:
    assert (destructive_reason(sql) is not None) is danger


def test_read_only_detection() -> None:
    assert is_read_only_statement("select 1") and is_read_only_statement("  EXPLAIN SELECT 1")
    assert is_read_only_statement("WITH x AS (SELECT 1) SELECT * FROM x")
    assert not is_read_only_statement("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d")
    assert not is_read_only_statement("INSERT INTO t VALUES (1)")
    text = "SELECT 'drop' -- drop\n"
    assert code_only(text) == "SELECT" + " " * (len(text) - 7) + "\n"  # same length, strings/comments blanked


def test_identifier_quoting() -> None:
    assert quote_ident("mysql", "we`ird") == "`we``ird`"
    assert quote_ident("postgres", 'billing.in"v') == '"billing"."in""v"'
    assert quote_ident("postgres", "users") == '"users"'


def test_profiles_and_history_store_no_passwords(tmp_path: Path) -> None:
    profiles = ProfileStore(tmp_path / "sql_connections.json")
    p = SqlProfile(kind="postgres", port=5432, user="app", database="shop", read_only=True, remember=True)
    profiles.remember("db-prod", p)
    profiles.remember("db-prod", SqlProfile(kind="postgres", port=5432, user="app", database="other"))  # same key
    stored = profiles.profiles("db-prod")
    assert len(stored) == 1 and stored[0].database == "other"
    assert "password" not in (tmp_path / "sql_connections.json").read_text()
    assert p.key("db-prod") == "sql:db-prod:postgres:app@127.0.0.1:5432"
    assert SqlProfile.from_dict({"kind": "oracle", "port": 99999}).kind == "mysql"  # sanitised

    history = HistoryStore(tmp_path / "sql_history.json")
    for i in range(305):
        history.add("db-prod", HistoryEntry(f"SELECT {i}", "shop", "2026-10-13 10:00:00", i, "ok", 1))
    entries = history.entries("db-prod")
    assert len(entries) == 300 and entries[0].sql == "SELECT 304"
    assert history.entries("other") == []


# -- regressions from the security review ----------------------------------- #
@pytest.mark.parametrize("sql,dialect", [
    ("SET default_transaction_read_only = off", "postgres"),
    ("EXPLAIN ANALYZE DELETE FROM t", "postgres"),
    ("SET SESSION TRANSACTION READ WRITE", "mysql"),
    ("SET GLOBAL read_only = 0", "mysql"),
    ("SET PASSWORD = 'x'", "mysql"),
    ("SELECT * INTO OUTFILE '/tmp/x' FROM t", "mysql"),
    ("SELECT * INTO new_table FROM t", "postgres"),
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "postgres"),
])
def test_read_only_mode_cannot_be_bypassed(sql: str, dialect: str) -> None:
    assert not is_read_only_statement(sql, dialect)


def test_read_only_mode_allows_harmless_statements() -> None:
    for sql, dialect in (("SET NAMES utf8mb4", "mysql"), ("SET search_path TO public", "postgres"),
                         ("SELECT note FROM t WHERE note = 'into'", "mysql"), ("EXPLAIN SELECT 1", "mysql")):
        assert is_read_only_statement(sql, dialect), sql


def test_postgres_backslash_is_not_an_escape() -> None:
    parts = [t.strip() for _s, _e, t in split_statements("SELECT 'a\\'; DROP TABLE x; --'", "postgres")]
    assert len(parts) == 2 and parts[1].startswith("DROP TABLE x")
    assert destructive_reason(parts[1], "postgres") is not None  # so the confirmation is asked


def test_history_never_keeps_passwords() -> None:
    from ssh_terminal.services.sql_service import HISTORY_SQL_MAX, redact

    assert redact("CREATE USER 'a'@'%' IDENTIFIED BY 's3cret'") == "CREATE USER 'a'@'%' IDENTIFIED BY '***'"
    assert "p4ss" not in redact("ALTER USER bob WITH PASSWORD 'p4ss'")
    assert "x" not in redact("SET PASSWORD FOR bob = 'x'").split("=")[1]
    assert len(redact("SELECT " + "1," * 5000 + "1")) <= HISTORY_SQL_MAX + 2


def test_csv_export_neutralises_formulas() -> None:
    from ssh_terminal.ui.sql_pane import export_text

    text = export_text("csv", ["a", "b"], [("=HYPERLINK(\"x\")", 5), ("-1+1", None), ("ok", -3)])
    lines = text.splitlines()
    assert lines[1].startswith("\"'=HYPERLINK") and lines[2].startswith("'-1+1,")
    assert lines[3] == "ok,-3"  # numbers stay numbers


def test_database_candidate_keeps_the_published_ip() -> None:
    c = Container("1", "pg", "postgres:16", "Up", "running", "10.0.0.5:5433->5432/tcp")
    assert database_candidates([c])[0].host == "10.0.0.5"
