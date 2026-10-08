"""Effective configuration resolution.

OpenSSH merges every matching block ("first obtained value wins"), so the
effective configuration of ``production`` may come from ``Host production``,
``Host *.example.com`` and ``Host *``. :class:`SSHConfigResolver` asks OpenSSH
itself (``ssh -G <alias>``) when the binary is available and falls back to a
built-in implementation of the matching rules otherwise.
"""

from __future__ import annotations

import fnmatch
import getpass
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ssh_terminal.models.ssh_host import DEFAULT_SSH_PORT, SSHHost, parse_bool
from ssh_terminal.ssh.config_parser import BlockKind, ConfigBlock, LineKind, SSHConfigDocument, SSHConfigSet, split_arguments
from ssh_terminal.utils.paths import get_ssh_dir
from ssh_terminal.utils.platform import is_windows

log = logging.getLogger(__name__)

MULTI_VALUE_KEYS = {"identityfile", "localforward", "remoteforward", "dynamicforward", "certificatefile", "sendenv", "setenv"}
DEFAULT_IDENTITY_FILES = ["~/.ssh/id_rsa", "~/.ssh/id_ecdsa", "~/.ssh/id_ecdsa_sk", "~/.ssh/id_ed25519", "~/.ssh/id_ed25519_sk"]


@dataclass
class ResolvedConfig:
    """Effective parameters used to open a connection."""

    alias: str
    hostname: str
    user: str
    port: int = DEFAULT_SSH_PORT
    identity_files: list[str] = field(default_factory=list)
    identity_files_explicit: bool = False
    proxy_jump: str | None = None
    proxy_command: str | None = None
    forward_agent: bool = False
    forward_x11: bool = False
    compression: bool = False
    server_alive_interval: int = 0
    server_alive_count_max: int = 3
    strict_host_key_checking: str = "ask"
    preferred_authentications: list[str] = field(
        default_factory=lambda: ["publickey", "keyboard-interactive", "password"]
    )
    identities_only: bool = False
    add_keys_to_agent: str = "no"
    connect_timeout: int | None = None
    user_known_hosts_files: list[str] = field(default_factory=lambda: ["~/.ssh/known_hosts", "~/.ssh/known_hosts2"])
    global_known_hosts_files: list[str] = field(default_factory=list)
    host_key_alias: str | None = None
    local_forwards: list[str] = field(default_factory=list)
    remote_forwards: list[str] = field(default_factory=list)
    dynamic_forwards: list[str] = field(default_factory=list)
    source: str = "builtin"  # "openssh" | "builtin"

    @property
    def endpoint(self) -> str:
        return f"{self.user}@{self.hostname}:{self.port}"


# ---------------------------------------------------------------------- #
# OpenSSH binary
# ---------------------------------------------------------------------- #
def find_ssh_binary() -> str | None:
    """Locate the OpenSSH client (``ssh`` / ``ssh.exe``)."""
    exe = shutil.which("ssh.exe" if is_windows() else "ssh")
    if exe:
        return exe
    if is_windows():
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate = Path(system_root) / "System32" / "OpenSSH" / "ssh.exe"
        if candidate.exists():
            return str(candidate)
    else:
        for candidate in ("/usr/bin/ssh", "/usr/local/bin/ssh", "/bin/ssh"):
            if Path(candidate).exists():
                return candidate
    return None


def openssh_default_config() -> Path | None:
    """The user config file OpenSSH reads when no ``-F`` is given."""
    if is_windows():
        profile = os.environ.get("USERPROFILE")
        return Path(profile) / ".ssh" / "config" if profile else None
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".ssh" / "config"
    except (ImportError, KeyError):
        return None


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if is_windows() else 0


def openssh_version(binary: str | None = None) -> str | None:
    """Output of ``ssh -V`` (written to stderr by OpenSSH), or None."""
    exe = binary or find_ssh_binary()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-V"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5, creationflags=_no_window_flags()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ssh -V failed: %s", exc)
        return None
    out = (proc.stderr or proc.stdout).strip()
    return out.splitlines()[0] if out else None


def parse_ssh_g_output(alias: str, text: str) -> ResolvedConfig:
    """Interpret the output of ``ssh -G``."""
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        values.setdefault(key.lower(), []).append(value.strip())

    def first(key: str, default: str | None = None) -> str | None:
        v = values.get(key)
        return v[0] if v else default

    def opt(key: str) -> str | None:
        v = first(key)
        return None if v is None or v.lower() == "none" else v

    def to_int(key: str, default: int) -> int:
        try:
            return int(first(key, str(default)) or default)
        except ValueError:
            return default

    strict = (first("stricthostkeychecking", "ask") or "ask").lower()
    strict = {"true": "yes", "false": "no"}.get(strict, strict)
    prefs = first("preferredauthentications")
    timeout = first("connecttimeout")
    resolved = ResolvedConfig(
        alias=alias,
        hostname=first("hostname", alias) or alias,
        user=first("user") or getpass.getuser(),
        port=to_int("port", DEFAULT_SSH_PORT),
        identity_files=values.get("identityfile", []),
        proxy_jump=opt("proxyjump"),
        proxy_command=opt("proxycommand"),
        forward_agent=bool(parse_bool(first("forwardagent", "no"))),
        forward_x11=bool(parse_bool(first("forwardx11", "no"))),
        compression=bool(parse_bool(first("compression", "no"))),
        server_alive_interval=to_int("serveraliveinterval", 0),
        server_alive_count_max=to_int("serveralivecountmax", 3),
        strict_host_key_checking=strict,
        identities_only=bool(parse_bool(first("identitiesonly", "no"))),
        add_keys_to_agent=first("addkeystoagent", "no") or "no",
        connect_timeout=int(timeout) if timeout and timeout.isdigit() else None,
        user_known_hosts_files=_flatten(values.get("userknownhostsfile", [])) or ["~/.ssh/known_hosts"],
        global_known_hosts_files=_flatten(values.get("globalknownhostsfile", [])),
        host_key_alias=opt("hostkeyalias"),
        local_forwards=values.get("localforward", []),
        remote_forwards=values.get("remoteforward", []),
        dynamic_forwards=values.get("dynamicforward", []),
        source="openssh",
    )
    if prefs:
        resolved.preferred_authentications = [p.strip() for p in prefs.split(",") if p.strip()]
    return resolved


def _flatten(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        out.extend(split_arguments(item))
    return out


# ---------------------------------------------------------------------- #
# Built-in matching
# ---------------------------------------------------------------------- #
def host_matches(patterns: list[str], name: str) -> bool:
    """OpenSSH ``Host`` matching: any positive match and no negated match."""
    matched = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        pat = pattern[1:] if negate else pattern
        if fnmatch.fnmatchcase(name.lower(), pat.lower()):
            if negate:
                return False
            matched = True
    return matched


_TOKEN_RE = re.compile(r"%(.)")


def expand_tokens(value: str, *, hostname: str, port: int, user: str, alias: str) -> str:
    """Expand the ``%h %p %r %n %d %u %%`` tokens used in several options."""

    def repl(m: re.Match[str]) -> str:
        t = m.group(1)
        return {
            "h": hostname,
            "p": str(port),
            "r": user,
            "n": alias,
            "d": str(get_ssh_dir().parent),
            "u": getpass.getuser(),
            "%": "%",
        }.get(t, m.group(0))

    return _TOKEN_RE.sub(repl, value)


class SSHConfigResolver:
    """Resolve an alias into a :class:`ResolvedConfig`."""

    def __init__(self, config_path: Path, prefer_openssh: bool = True, custom_path: bool = False) -> None:
        self.config_path = config_path
        self.prefer_openssh = prefer_openssh
        self.custom_path = custom_path
        self._ssh_binary: str | None | bool = False  # False = not looked up yet

    @property
    def ssh_binary(self) -> str | None:
        if self._ssh_binary is False:
            self._ssh_binary = find_ssh_binary()
        return self._ssh_binary or None  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    def resolve(self, alias: str, config: SSHConfigSet | None = None) -> ResolvedConfig:
        if self.prefer_openssh and self.ssh_binary:
            try:
                return self.resolve_with_openssh(alias)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                log.warning("ssh -G failed for %s, using built-in resolver: %s", alias, exc)
        cfg = config or SSHConfigSet.load(self.config_path)
        return self.resolve_builtin(alias, cfg)

    def _needs_explicit_config(self) -> bool:
        """True if OpenSSH would not read :attr:`config_path` by itself.

        OpenSSH locates ``~/.ssh/config`` from the passwd database (POSIX) or
        the user profile (Windows), not from ``$HOME``; pass ``-F`` whenever
        that differs from the file we manage. (``-F`` skips the system-wide
        ``/etc/ssh/ssh_config``, so it is only used when needed.)
        """
        if self.custom_path:
            return True
        default = openssh_default_config()
        try:
            return default is None or default.resolve() != self.config_path.resolve()
        except OSError:
            return True

    def resolve_with_openssh(self, alias: str) -> ResolvedConfig:
        cmd = [self.ssh_binary or "ssh"]
        if self._needs_explicit_config():
            cmd += ["-F", str(self.config_path)]
        cmd += ["-G", "--", alias]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, creationflags=_no_window_flags())
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"ssh -G exited with {proc.returncode}")
        resolved = parse_ssh_g_output(alias, proc.stdout)
        explicit = self._explicit_identity_files(alias)
        resolved.identity_files_explicit = bool(explicit)
        log.debug("Resolved %s via OpenSSH: %s", alias, resolved.endpoint)
        return resolved

    def _explicit_identity_files(self, alias: str) -> list[str]:
        try:
            cfg = SSHConfigSet.load(self.config_path)
        except OSError:
            return []
        return self._collect(alias, cfg.ordered_blocks()).get("identityfile", [])

    # ------------------------------------------------------------------ #
    def resolve_builtin(self, alias: str, config: SSHConfigSet, override: SSHHost | None = None) -> ResolvedConfig:
        """Resolve using our own parser.

        ``override`` lets the UI resolve a host that is being edited (not yet
        saved): its block is evaluated first and the saved block with the same
        alias is ignored.
        """
        blocks = config.ordered_blocks()
        if override is not None:
            from ssh_terminal.ssh.config_writer import build_block

            fake_doc = SSHConfigDocument(path=None)
            fake = build_block(override)
            skip = {id(b) for _, b in blocks if b.kind is BlockKind.HOST and override.alias in b.patterns}
            blocks = [(fake_doc, fake)] + [(d, b) for d, b in blocks if id(b) not in skip]
        values = self._collect(alias, blocks)
        return self._to_resolved(alias, values)

    def _collect(self, alias: str, blocks: list[tuple[SSHConfigDocument, ConfigBlock]]) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for _doc, block in blocks:
            if block.kind is BlockKind.MATCH:
                criteria = split_arguments(block.header.value.lower()) if block.header else []
                if criteria != ["all"]:
                    continue  # Match is only evaluated by OpenSSH itself
            elif block.kind is BlockKind.HOST and not host_matches(block.patterns, alias):
                continue
            for line in block.lines:
                if line.kind is not LineKind.OPTION:
                    continue
                key = line.keyword
                if key == "include":
                    continue
                value = line.value.strip()
                if key in MULTI_VALUE_KEYS:
                    values.setdefault(key, []).append(_strip_quotes(value))
                elif key not in values:
                    values[key] = [value if key == "proxycommand" else _strip_quotes(value)]
        return values

    def _to_resolved(self, alias: str, values: dict[str, list[str]]) -> ResolvedConfig:
        def first(key: str) -> str | None:
            v = values.get(key)
            return v[0] if v else None

        def to_int(key: str, default: int) -> int:
            try:
                return int(first(key) or default)
            except ValueError:
                return default

        hostname = first("hostname") or alias
        port = to_int("port", DEFAULT_SSH_PORT)
        user = first("user") or getpass.getuser()
        hostname = expand_tokens(hostname, hostname=alias, port=port, user=user, alias=alias)
        proxy_jump = first("proxyjump")
        proxy_command = first("proxycommand")
        identity = values.get("identityfile", [])
        prefs = first("preferredauthentications")
        strict = (first("stricthostkeychecking") or "ask").lower()
        timeout = first("connecttimeout")
        resolved = ResolvedConfig(
            alias=alias,
            hostname=hostname,
            user=user,
            port=port,
            identity_files=[expand_tokens(i, hostname=hostname, port=port, user=user, alias=alias) for i in identity]
            or list(DEFAULT_IDENTITY_FILES),
            identity_files_explicit=bool(identity),
            proxy_jump=None if (proxy_jump or "").lower() == "none" else proxy_jump,
            proxy_command=None if (proxy_command or "").lower() == "none" else proxy_command,
            forward_agent=bool(parse_bool(first("forwardagent") or "no")),
            forward_x11=bool(parse_bool(first("forwardx11") or "no")),
            compression=bool(parse_bool(first("compression") or "no")),
            server_alive_interval=to_int("serveraliveinterval", 0),
            server_alive_count_max=to_int("serveralivecountmax", 3),
            strict_host_key_checking=strict,
            identities_only=bool(parse_bool(first("identitiesonly") or "no")),
            add_keys_to_agent=first("addkeystoagent") or "no",
            connect_timeout=int(timeout) if timeout and timeout.isdigit() else None,
            user_known_hosts_files=_flatten(values.get("userknownhostsfile", [])) or ["~/.ssh/known_hosts", "~/.ssh/known_hosts2"],
            host_key_alias=first("hostkeyalias"),
            local_forwards=values.get("localforward", []),
            remote_forwards=values.get("remoteforward", []),
            dynamic_forwards=values.get("dynamicforward", []),
            source="builtin",
        )
        if prefs:
            resolved.preferred_authentications = [p.strip() for p in prefs.split(",") if p.strip()]
        return resolved


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1]
    return v


def resolve_jump_spec(spec: str) -> tuple[str | None, str, int | None]:
    """Split a ProxyJump element ``[user@]host[:port]`` (or ``ssh://...``)."""
    s = spec.strip()
    if s.startswith("ssh://"):
        s = s[len("ssh://") :]
    user: str | None = None
    if "@" in s:
        user, _, s = s.rpartition("@")
    port: int | None = None
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif s.count(":") == 1:
        host, _, p = s.partition(":")
        port = int(p) if p.isdigit() else None
    else:
        host = s
    return user, host, port
