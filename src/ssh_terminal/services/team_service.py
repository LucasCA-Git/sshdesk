"""Team sharing: talks to an SSHDesk Server and materialises team hosts as SSH config.

How team hosts reach your machine
---------------------------------
1. ``sync()`` downloads the hosts of every team you belong to.
2. Each team is written to ``<app dir>/teams/<slug>.conf`` in plain OpenSSH
   syntax (managed files: overwritten on every sync). Host keys published by the
   team go to ``<app dir>/teams/known_hosts``.
3. One ``Include`` line at the top of ``~/.ssh/config`` pulls those files in, so
   team hosts also work with plain ``ssh``, ``scp`` and ``git``.

Your personal ``~/.ssh/config`` stays yours: a team host whose alias already
exists in it is skipped (reported as a conflict) instead of overriding it.
Nothing secret is ever uploaded: shared hosts contain no keys or passwords.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ssh_terminal import __version__
from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.services.host_identity import alias_key, same_server
from ssh_terminal.ssh.config_parser import ConfigLine, LineKind, SSHConfigDocument, SSHConfigSet, quote_argument
from ssh_terminal.ssh.config_writer import ConfigFileWriter
from ssh_terminal.utils.paths import expand_user_path, get_app_dir

log = logging.getLogger(__name__)

KEYRING_PREFIX = "team-token:"
INCLUDE_MARKER = "# SSHDesk teams (managed by SSHDesk, do not edit the files it includes)"

# Same allow-list as the server (defence in depth: never write anything that
# could execute commands on this machine, whatever the server sends).
ALLOWED_OPTIONS = {
    "addkeystoagent", "ciphers", "compression", "connecttimeout", "dynamicforward", "forwardagent",
    "forwardx11", "hostkeyalgorithms", "identitiesonly", "kexalgorithms", "localforward", "loglevel", "macs",
    "preferredauthentications", "pubkeyacceptedalgorithms", "remotecommand", "remoteforward", "requesttty",
    "sendenv", "serveralivecountmax", "serveraliveinterval", "setenv", "tcpkeepalive",
}
_SAFE = {
    "alias": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$"),
    "hostname": re.compile(r"^[A-Za-z0-9._:%\[\]-]{1,255}$"),
    "user": re.compile(r"^[A-Za-z0-9._@-]{0,120}$"),
    "proxy_jump": re.compile(r"^[A-Za-z0-9._@:,\[\]-]{0,255}$"),
}
_HOST_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-[a-z0-9-]+@openssh\.com) [A-Za-z0-9+/]+={0,3}$")


class TeamApiError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Account:
    server: str
    email: str
    name: str = ""
    user_id: int = 0


@dataclass
class TeamInfo:
    id: int
    name: str
    slug: str
    role: str
    revision: int = 0
    members: int = 0
    hosts: int = 0

    @property
    def can_edit(self) -> bool:
        return self.role in ("owner", "admin")


@dataclass
class SyncResult:
    teams: list[TeamInfo] = field(default_factory=list)
    hosts_written: int = 0
    conflicts: list[str] = field(default_factory=list)  # "alias (team)"
    rejected: list[str] = field(default_factory=list)  # hosts refused by local validation
    unchanged: bool = False
    include_added: bool = False
    hidden: int = 0  # team hosts this user removed from their own list


# ---------------------------------------------------------------------- #
# HTTP
# ---------------------------------------------------------------------- #
class TeamClient:
    """Tiny JSON client for the SSHDesk Server API (stdlib only)."""

    def __init__(self, server: str, token: str | None = None, timeout: float = 15.0) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
        url = f"{self.server}/api/v1{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", f"SSHDesk/{__version__}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - user-configured server URL
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, None, dict(exc.headers)
            raise TeamApiError(self._error_message(exc), exc.code) from None
        except urllib.error.URLError as exc:
            raise TeamApiError(f"Cannot reach the team server {self.server}: {exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise TeamApiError(f"Cannot reach the team server {self.server}: {exc}") from None
        except json.JSONDecodeError:
            raise TeamApiError(f"{self.server} is not an SSHDesk server (invalid response)") from None

    @staticmethod
    def _error_message(exc: urllib.error.HTTPError) -> str:
        try:
            detail = json.loads(exc.read() or b"{}").get("detail")
        except (json.JSONDecodeError, OSError, AttributeError):
            detail = None
        if isinstance(detail, list):  # pydantic validation errors
            detail = "; ".join(f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail)
        return str(detail or exc.reason or f"HTTP {exc.code}")

    def get(self, path: str) -> Any:
        return self.request("GET", path)[1]

    def post(self, path: str, body: Any = None) -> Any:
        return self.request("POST", path, body if body is not None else {})[1]

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body)[1]

    def delete(self, path: str) -> None:
        self.request("DELETE", path)


# ---------------------------------------------------------------------- #
# Rendering team hosts as SSH config
# ---------------------------------------------------------------------- #
def validate_remote_host(host: dict[str, Any]) -> str | None:
    """Return an error message if a host from the server is unsafe to write."""
    for key, pattern in _SAFE.items():
        value = str(host.get(key) or "")
        if key in ("alias", "hostname") and not value:
            return f"missing {key}"
        if value and not pattern.match(value):
            return f"invalid {key}"
    port = host.get("port", 22)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return "invalid port"
    identity = str(host.get("identity_file") or "")
    if any(ord(c) < 32 or c in '"#' for c in identity):
        return "invalid identity_file"
    for option in host.get("options") or []:
        if not isinstance(option, (list, tuple)) or len(option) != 2:
            return "invalid option"
        key, value = str(option[0]), str(option[1])
        if key.lower() not in ALLOWED_OPTIONS or not key.isalnum():
            return f"option {key} not allowed"
        if any(ord(c) < 32 or c in '"#' for c in value):
            return f"invalid value for {key}"
    for key in host.get("host_keys") or []:
        if not _HOST_KEY.match(str(key)):
            return "invalid host key"
    return None


def render_team_config(team: dict[str, Any], hosts: list[dict[str, Any]], known_hosts_file: Path) -> str:
    lines = [
        f"# SSHDesk team: {team['name']} (role: {team['role']})",
        "# Managed by SSHDesk. This file is overwritten on every sync; edit hosts in SSHDesk (Teams) instead.",
        "",
    ]
    known = quote_argument(known_hosts_file.as_posix())
    for h in hosts:
        if h.get("description"):
            for desc_line in str(h["description"]).splitlines()[:5]:
                lines.append(f"# {desc_line}")
        lines.append(f"Host {h['alias']}")
        lines.append(f"    HostName {h['hostname']}")
        if h.get("user"):
            lines.append(f"    User {h['user']}")
        if h.get("port", 22) != 22:
            lines.append(f"    Port {h['port']}")
        if h.get("proxy_jump"):
            lines.append(f"    ProxyJump {h['proxy_jump']}")
        if h.get("identity_file"):
            lines.append(f"    IdentityFile {quote_argument(h['identity_file'])}")
        if h.get("host_keys"):
            lines.append(f"    UserKnownHostsFile ~/.ssh/known_hosts {known}")
        for key, value in h.get("options") or []:
            lines.append(f"    {key} {value}")
        lines.append("")
    return "\n".join(lines)


def known_hosts_lines(hosts: list[dict[str, Any]]) -> list[str]:
    out = []
    for h in hosts:
        port = h.get("port", 22)
        host_id = h["hostname"] if port == 22 else f"[{h['hostname']}]:{port}"
        for key in h.get("host_keys") or []:
            out.append(f"{host_id} {key}")
    return out


def host_to_payload(host: SSHHost, group: str = "", host_keys: list[str] | None = None) -> dict[str, Any]:
    """Personal host -> team host payload (only shareable fields, never secrets)."""
    options: list[list[str]] = []
    for key, value in [
        ("ServerAliveInterval", host.server_alive_interval),
        ("ServerAliveCountMax", host.server_alive_count_max),
        ("ForwardAgent", None if host.forward_agent is None else ("yes" if host.forward_agent else "no")),
        ("Compression", None if host.compression is None else ("yes" if host.compression else "no")),
        ("IdentitiesOnly", None if host.identities_only is None else ("yes" if host.identities_only else "no")),
        ("PreferredAuthentications", host.preferred_authentications),
    ]:
        if value is not None:
            options.append([key, str(value)])
    for spec in host.local_forwards:
        options.append(["LocalForward", spec])
    for spec in host.remote_forwards:
        options.append(["RemoteForward", spec])
    for spec in host.dynamic_forwards:
        options.append(["DynamicForward", spec])
    for key, value in host.extra_options:
        if key.lower() in ALLOWED_OPTIONS:
            options.append([key, value])
    return {
        "alias": host.alias,
        "hostname": host.target_host,
        "user": host.user or "",
        "port": host.effective_port,
        "proxy_jump": host.proxy_jump or "",
        "identity_file": host.identity_files[0] if host.identity_files else "",
        "group": group,
        "description": host.comment or "",
        "options": options,
        "host_keys": host_keys or [],
    }


# ---------------------------------------------------------------------- #
# Service
# ---------------------------------------------------------------------- #
class TeamService:
    def __init__(self, credentials: Any, app_dir: Path | None = None) -> None:
        self.credentials = credentials  # KeyringCredentialStore
        self.dir = (app_dir or get_app_dir()) / "teams"
        self.state_file = self.dir / "teams.json"
        self.account: Account | None = None
        self.teams: list[TeamInfo] = []
        self._token: str | None = None
        self._etag: str | None = None
        self.host_groups: dict[str, str] = {}  # team host alias -> group name (from the last sync)
        self.hidden_hosts: set[str] = set()  # "team-slug/alias" removed by this user from their own list
        self._load_state()

    # -- persistence of non-secret state ---------------------------------- #
    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        acc = data.get("account")
        if isinstance(acc, dict) and acc.get("server") and acc.get("email"):
            self.account = Account(acc["server"], acc["email"], acc.get("name", ""), int(acc.get("user_id", 0)))
            self._token = self._keyring_get(acc["server"], acc["email"])
        self.teams = [TeamInfo(**t) for t in data.get("teams", []) if isinstance(t, dict)]
        self._etag = data.get("etag")
        groups = data.get("host_groups")
        if isinstance(groups, dict):
            self.host_groups = {str(k): str(v) for k, v in groups.items()}
        hidden = data.get("hidden_hosts")
        if isinstance(hidden, list):
            self.hidden_hosts = {str(x) for x in hidden}

    # -- hosts removed from "my list" (local only, never sent to the server) -- #
    @staticmethod
    def _hidden_key(slug: str, alias: str) -> str:
        return f"{slug}/{alias_key(alias)}"

    def is_hidden(self, slug: str, alias: str) -> bool:
        return self._hidden_key(slug, alias) in self.hidden_hosts

    def hide_host(self, team: TeamInfo, alias: str, writer: ConfigFileWriter | None = None) -> None:
        """Remove a team host from this user's list (and from the managed config file right away)."""
        self.hidden_hosts.add(self._hidden_key(team.slug, alias))
        self._save_state()
        path = self.team_file(team.slug)
        if path.exists():
            from ssh_terminal.ssh import config_writer

            doc = SSHConfigDocument.load(path)
            block = next((b for b in doc.host_blocks() if alias_key(alias) in {alias_key(p) for p in b.patterns}), None)
            if block is not None:
                config_writer.remove_host(doc, block.patterns[0])
                path.write_text(doc.render(), encoding="utf-8")

    def hidden_count(self, team: TeamInfo | None = None) -> int:
        if team is None:
            return len(self.hidden_hosts)
        return sum(1 for k in self.hidden_hosts if k.startswith(f"{team.slug}/"))

    def restore_hidden(self, team: TeamInfo | None = None) -> int:
        """Bring hidden team hosts back (on the next sync). Returns how many were restored."""
        before = len(self.hidden_hosts)
        if team is None:
            self.hidden_hosts.clear()
        else:
            self.hidden_hosts = {k for k in self.hidden_hosts if not k.startswith(f"{team.slug}/")}
        self._etag = None  # force a full download
        self._save_state()
        return before - len(self.hidden_hosts)

    def group_of(self, alias: str) -> str:
        """Group a team host was shared under ("" = none)."""
        return self.host_groups.get(alias, "")

    def _save_state(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {
            "account": self.account.__dict__ if self.account else None,
            "teams": [t.__dict__ for t in self.teams],
            "etag": self._etag,
            "host_groups": self.host_groups,
            "hidden_hosts": sorted(self.hidden_hosts),
        }
        self.state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _keyring_name(self, server: str, email: str) -> str:
        return f"{KEYRING_PREFIX}{email}@{server}"

    def _keyring_get(self, server: str, email: str) -> str | None:
        if not getattr(self.credentials, "available", False):
            return None
        return self.credentials._get(self._keyring_name(server, email))  # noqa: SLF001 - same package

    def _keyring_set(self, server: str, email: str, token: str) -> None:
        if getattr(self.credentials, "available", False):
            self.credentials._set(self._keyring_name(server, email), token)  # noqa: SLF001

    def _keyring_delete(self, server: str, email: str) -> None:
        if getattr(self.credentials, "available", False):
            self.credentials._delete(self._keyring_name(server, email))  # noqa: SLF001

    # -- account ------------------------------------------------------------ #
    @property
    def signed_in(self) -> bool:
        return self.account is not None and bool(self._token)

    def client(self, server: str | None = None) -> TeamClient:
        if server is None:
            if self.account is None:
                raise TeamApiError("Not signed in")
            server = self.account.server
        return TeamClient(server, self._token if server == (self.account.server if self.account else None) else None)

    def server_info(self, server: str) -> dict[str, Any]:
        return TeamClient(server).get("/info")

    def _signed_in(self, server: str, payload: dict[str, Any]) -> Account:
        user = payload["user"]
        self.account = Account(server.rstrip("/"), user["email"], user.get("name", ""), int(user["id"]))
        self._token = payload["access_token"]
        self._keyring_set(self.account.server, self.account.email, self._token)
        self._etag = None
        self._save_state()
        log.info("Signed in to team server %s as %s", self.account.server, self.account.email)
        return self.account

    def register(self, server: str, email: str, password: str, name: str = "") -> Account:
        payload = TeamClient(server).post("/auth/register", {"email": email, "password": password, "name": name})
        return self._signed_in(server, payload)

    def login(self, server: str, email: str, password: str) -> Account:
        payload = TeamClient(server).post("/auth/login", {"email": email, "password": password})
        return self._signed_in(server, payload)

    def logout(self) -> None:
        if self.account:
            self._keyring_delete(self.account.server, self.account.email)
        self.account = None
        self._token = None
        self.teams = []
        self._etag = None
        self.host_groups = {}
        self.hidden_hosts = set()
        self.clear_team_files()
        self._save_state()

    # -- teams ----------------------------------------------------------------- #
    def list_teams(self) -> list[TeamInfo]:
        self.teams = [TeamInfo(**t) for t in self.client().get("/teams")]
        self._save_state()
        return self.teams

    def create_team(self, name: str) -> TeamInfo:
        return TeamInfo(**self.client().post("/teams", {"name": name}))

    def members(self, team_id: int) -> list[dict[str, Any]]:
        return self.client().get(f"/teams/{team_id}/members")

    def set_role(self, team_id: int, user_id: int, role: str) -> None:
        self.client().put(f"/teams/{team_id}/members/{user_id}", {"role": role})

    def remove_member(self, team_id: int, user_id: int) -> None:
        self.client().delete(f"/teams/{team_id}/members/{user_id}")

    def leave(self, team_id: int) -> None:
        assert self.account is not None
        self.remove_member(team_id, self.account.user_id)

    def delete_team(self, team_id: int) -> None:
        self.client().delete(f"/teams/{team_id}")

    def invite(self, team_id: int, email: str, role: str = "member") -> dict[str, Any]:
        return self.client().post(f"/teams/{team_id}/invites", {"email": email, "role": role})

    def team_invites(self, team_id: int) -> list[dict[str, Any]]:
        return self.client().get(f"/teams/{team_id}/invites")

    def revoke_invite(self, team_id: int, invite_id: int) -> None:
        self.client().delete(f"/teams/{team_id}/invites/{invite_id}")

    def my_invites(self) -> list[dict[str, Any]]:
        return self.client().get("/invites")

    def accept_invite(self, code: str) -> TeamInfo:
        return TeamInfo(**self.client().post("/invites/accept", {"code": code}))

    # -- hosts -------------------------------------------------------------- #
    def team_hosts(self, team_id: int) -> list[dict[str, Any]]:
        return self.client().get(f"/teams/{team_id}/hosts")

    def save_host(self, team_id: int, payload: dict[str, Any], host_id: int | None = None) -> dict[str, Any]:
        if host_id:
            return self.client().put(f"/teams/{team_id}/hosts/{host_id}", payload)
        return self.client().post(f"/teams/{team_id}/hosts", payload)

    def delete_host(self, team_id: int, host_id: int) -> None:
        self.client().delete(f"/teams/{team_id}/hosts/{host_id}")

    # -- local files ---------------------------------------------------------- #
    def team_file(self, slug: str) -> Path:
        return self.dir / f"{slug}.conf"

    @property
    def known_hosts_file(self) -> Path:
        return self.dir / "known_hosts"

    def team_for_path(self, path: Path | None) -> TeamInfo | None:
        if path is None:
            return None
        try:
            if path.resolve().parent != self.dir.resolve():
                return None
        except OSError:
            return None
        return next((t for t in self.teams if self.team_file(t.slug).name == path.name), None)

    def clear_team_files(self) -> None:
        if not self.dir.exists():
            return
        for path in self.dir.glob("*.conf"):
            path.unlink(missing_ok=True)
        self.known_hosts_file.unlink(missing_ok=True)

    def include_pattern(self) -> str:
        return (self.dir / "*.conf").as_posix()

    def ensure_include(self, config_path: Path, writer: ConfigFileWriter) -> bool:
        """Add ``Include <app dir>/teams/*.conf`` at the top of the SSH config. Returns True if added."""
        doc = SSHConfigDocument.load(config_path)
        target = Path(self.include_pattern())
        for _block, pattern in doc.include_patterns():
            if expand_user_path(pattern) == target:
                return False
        include = ConfigLine.option("Include", quote_argument(self.include_pattern()), indent="")
        marker = ConfigLine(INCLUDE_MARKER, LineKind.COMMENT)
        lines = doc.global_block.lines
        # Keep the file's header comments first, then our Include (it must be
        # before the first Host block to apply globally).
        insert_at = 0
        while insert_at < len(lines) and lines[insert_at].kind is LineKind.COMMENT:
            insert_at += 1
        blank = ConfigLine("", LineKind.BLANK)
        lines[insert_at:insert_at] = [marker, include, blank]
        doc.has_final_newline = True
        writer.write(config_path, doc.render())
        log.info("Added team Include to %s", config_path)
        return True

    # -- sync -------------------------------------------------------------- #
    def sync(self, config_path: Path, writer: ConfigFileWriter, force: bool = False) -> SyncResult:
        """Download team hosts and write the managed config files."""
        result = SyncResult()
        client = self.client()
        headers = {} if force or not self._etag else {"If-None-Match": self._etag}
        status, payload, resp_headers = client.request("GET", "/sync", headers=headers)
        if status == 304 and any(self.team_file(t.slug).exists() for t in self.teams):
            result.unchanged = True
            result.teams = list(self.teams)
            return result
        if payload is None:  # 304 but files missing (e.g. deleted): fetch again
            status, payload, resp_headers = client.request("GET", "/sync")
        self._etag = resp_headers.get("ETag") or resp_headers.get("etag")
        teams = payload["teams"]

        # The user's own hosts always win: a team host is skipped when it has the
        # same name OR points at the same server as a personal host. Between
        # teams, the first team that provides a host wins.
        personal = SSHConfigSet.load(config_path)
        own_hosts = [h for doc in personal.documents if not self._is_team_file(doc.path) for h in doc.hosts()]
        own_aliases = {alias_key(p) for h in own_hosts for p in h.patterns}
        own_servers = [h for h in own_hosts if not h.is_wildcard]
        accepted: list[tuple[str, dict[str, Any]]] = []  # (team name, host) already written

        self.dir.mkdir(parents=True, exist_ok=True)
        wanted_files: set[str] = set()
        all_known: list[str] = []
        seen_aliases: set[str] = set()
        self.teams = []
        self.host_groups = {}
        for team in teams:
            info = TeamInfo(team["id"], team["name"], team["slug"], team["role"], team["revision"], 0, len(team["hosts"]))
            self.teams.append(info)
            usable = []
            for host in team["hosts"]:
                error = validate_remote_host(host)
                if error:
                    result.rejected.append(f"{host.get('alias', '?')} ({team['name']}): {error}")
                    continue
                if self.is_hidden(team["slug"], host["alias"]):
                    result.hidden += 1
                    continue
                reason = self._duplicate_reason(host, own_aliases, own_servers, seen_aliases, accepted)
                if reason is not None:
                    result.conflicts.append(f"{host['alias']} ({team['name']}){reason}")
                    continue
                seen_aliases.add(alias_key(host["alias"]))
                accepted.append((team["name"], host))
                usable.append(host)
                if host.get("group"):
                    self.host_groups[host["alias"]] = " ".join(str(host["group"]).split())
            path = self.team_file(team["slug"])
            path.write_text(render_team_config(team, usable, self.known_hosts_file), encoding="utf-8")
            wanted_files.add(path.name)
            all_known.extend(known_hosts_lines(usable))
            result.hosts_written += len(usable)
        for stale in self.dir.glob("*.conf"):
            if stale.name not in wanted_files:
                stale.unlink(missing_ok=True)
        self.known_hosts_file.write_text("\n".join(all_known) + ("\n" if all_known else ""), encoding="utf-8")
        if teams:
            result.include_added = self.ensure_include(config_path, writer)
        result.teams = list(self.teams)
        self._save_state()
        log.info("Team sync: %d team(s), %d host(s), %d conflict(s)", len(teams), result.hosts_written, len(result.conflicts))
        return result

    @staticmethod
    def _duplicate_reason(host: dict[str, Any], own_aliases: set[str], own_servers: list[SSHHost],
                          seen_aliases: set[str], accepted: list[tuple[str, dict[str, Any]]]) -> str | None:
        """Why a team host must not be written ("" = same name as a personal host), or None if it is new."""
        name = alias_key(host["alias"])
        if name in own_aliases:
            return ""
        mine = next((h for h in own_servers if same_server(host, h)), None)
        if mine is not None:
            return f": same server as your “{mine.alias}”"
        if name in seen_aliases:
            other = next(t for t, h in accepted if alias_key(h["alias"]) == name)
            return f": name already used by team “{other}”"
        twin = next(((t, h) for t, h in accepted if same_server(host, h)), None)
        if twin is not None:
            return f": same server as “{twin[1]['alias']}” from team “{twin[0]}”"
        return None

    def _is_team_file(self, path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.resolve().parent == self.dir.resolve() and path.suffix == ".conf"
        except OSError:
            return False
