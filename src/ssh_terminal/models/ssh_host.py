"""Data model for a ``Host`` block of the OpenSSH client configuration."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SSH_PORT = 22

# Canonical spelling of every keyword the application understands and edits
# through dedicated fields. Everything else is kept in ``extra_options``.
KNOWN_KEYWORDS: dict[str, str] = {
    "hostname": "HostName",
    "user": "User",
    "port": "Port",
    "identityfile": "IdentityFile",
    "proxyjump": "ProxyJump",
    "proxycommand": "ProxyCommand",
    "forwardagent": "ForwardAgent",
    "forwardx11": "ForwardX11",
    "compression": "Compression",
    "serveraliveinterval": "ServerAliveInterval",
    "serveralivecountmax": "ServerAliveCountMax",
    "stricthostkeychecking": "StrictHostKeyChecking",
    "preferredauthentications": "PreferredAuthentications",
    "identitiesonly": "IdentitiesOnly",
    "addkeystoagent": "AddKeysToAgent",
    "localforward": "LocalForward",
    "remoteforward": "RemoteForward",
    "dynamicforward": "DynamicForward",
}

TRUE_VALUES = {"yes", "true", "on", "1"}
FALSE_VALUES = {"no", "false", "off", "0"}


def parse_bool(value: str | None) -> bool | None:
    """OpenSSH yes/no flag -> bool. Unknown values (e.g. ``confirm``) map to True."""
    if value is None:
        return None
    v = value.strip().strip('"').lower()
    if v in FALSE_VALUES:
        return False
    if v in TRUE_VALUES:
        return True
    return True


def format_bool(value: bool) -> str:
    return "yes" if value else "no"


def is_pattern(token: str) -> bool:
    """True if a ``Host`` token is a wildcard or negated pattern."""
    return token.startswith("!") or any(ch in token for ch in "*?")


@dataclass
class SSHHost:
    """One ``Host`` block.

    ``None`` means "not set in this block" (OpenSSH will use the default or a
    value inherited from another matching block). Effective values are obtained
    through :class:`ssh_terminal.ssh.resolver.SSHConfigResolver`.
    """

    alias: str
    aliases: list[str] = field(default_factory=list)  # extra patterns on the Host line
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    identity_files: list[str] = field(default_factory=list)
    proxy_jump: str | None = None
    proxy_command: str | None = None
    forward_agent: bool | None = None
    forward_x11: bool | None = None
    compression: bool | None = None
    server_alive_interval: int | None = None
    server_alive_count_max: int | None = None
    strict_host_key_checking: str | None = None
    preferred_authentications: str | None = None
    identities_only: bool | None = None
    add_keys_to_agent: str | None = None
    local_forwards: list[str] = field(default_factory=list)
    remote_forwards: list[str] = field(default_factory=list)
    dynamic_forwards: list[str] = field(default_factory=list)
    # Unknown options, in file order. A list (not a dict) because OpenSSH
    # allows repeating keywords (SendEnv, CertificateFile, ...).
    extra_options: list[tuple[str, str]] = field(default_factory=list)
    source_file: Path | None = None
    is_wildcard: bool = False
    comment: str | None = None  # leading "# ..." comment block, display only

    # ------------------------------------------------------------------ #
    @property
    def patterns(self) -> list[str]:
        """All tokens of the ``Host`` line."""
        return [self.alias, *self.aliases]

    @property
    def effective_port(self) -> int:
        return self.port or DEFAULT_SSH_PORT

    @property
    def target_host(self) -> str:
        """HostName if set, otherwise the alias itself (OpenSSH behaviour)."""
        return self.hostname or self.alias

    @property
    def display_address(self) -> str:
        """``user@host:port`` style subtitle for the UI."""
        host = self.target_host
        user = f"{self.user}@" if self.user else ""
        port = f":{self.port}" if self.port and self.port != DEFAULT_SSH_PORT else ""
        return f"{user}{host}{port}"

    def matches_search(self, query: str, group: str | None = None) -> bool:
        """Case-insensitive search on alias, aliases, hostname, user and group."""
        q = query.strip().lower()
        if not q:
            return True
        haystack = [self.alias, *self.aliases, self.hostname or "", self.user or "", group or ""]
        return any(q in item.lower() for item in haystack)

    def clone(self, new_alias: str | None = None) -> SSHHost:
        dup = copy.deepcopy(self)
        if new_alias is not None:
            dup.alias = new_alias
            dup.aliases = []
        return dup

    def extra(self, keyword: str) -> str | None:
        """First value of an unknown option (case-insensitive)."""
        k = keyword.lower()
        for key, value in self.extra_options:
            if key.lower() == k:
                return value
        return None

    # ------------------------------------------------------------------ #
    def to_options(self) -> list[tuple[str, str]]:
        """Ordered ``(Keyword, value)`` pairs as they should appear in the file."""
        opts: list[tuple[str, str]] = []

        def add(key: str, value: object | None) -> None:
            if value is None or value == "":
                return
            if isinstance(value, bool):
                opts.append((KNOWN_KEYWORDS[key], format_bool(value)))
            else:
                opts.append((KNOWN_KEYWORDS[key], str(value)))

        add("hostname", self.hostname)
        add("user", self.user)
        add("port", self.port)
        for identity in self.identity_files:
            add("identityfile", identity)
        add("identitiesonly", self.identities_only)
        add("proxyjump", self.proxy_jump)
        add("proxycommand", self.proxy_command)
        add("forwardagent", self.forward_agent)
        add("forwardx11", self.forward_x11)
        add("compression", self.compression)
        add("serveraliveinterval", self.server_alive_interval)
        add("serveralivecountmax", self.server_alive_count_max)
        add("stricthostkeychecking", self.strict_host_key_checking)
        add("preferredauthentications", self.preferred_authentications)
        add("addkeystoagent", self.add_keys_to_agent)
        for spec in self.local_forwards:
            add("localforward", spec)
        for spec in self.remote_forwards:
            add("remoteforward", spec)
        for spec in self.dynamic_forwards:
            add("dynamicforward", spec)
        opts.extend(self.extra_options)
        return opts

    @classmethod
    def from_options(
        cls,
        patterns: list[str],
        options: list[tuple[str, str]],
        source_file: Path | None = None,
    ) -> SSHHost:
        """Build a host from the tokens of a ``Host`` line and its options.

        Only the *first* occurrence of single-valued keywords is used, matching
        OpenSSH's "first obtained value wins" rule.
        """
        concrete = [p for p in patterns if not is_pattern(p)]
        alias = concrete[0] if concrete else (patterns[0] if patterns else "*")
        aliases = [p for p in patterns if p != alias]
        host = cls(alias=alias, aliases=aliases, source_file=source_file, is_wildcard=not concrete)
        seen: set[str] = set()
        for key, value in options:
            k = key.lower()
            v = value
            if k not in KNOWN_KEYWORDS:
                host.extra_options.append((key, value))
                continue
            if k == "identityfile":
                host.identity_files.append(_unquote(v))
                continue
            if k == "localforward":
                host.local_forwards.append(v)
                continue
            if k == "remoteforward":
                host.remote_forwards.append(v)
                continue
            if k == "dynamicforward":
                host.dynamic_forwards.append(v)
                continue
            if k in seen:
                # Duplicate single-valued option: OpenSSH ignores it, we keep it
                # verbatim so nothing is lost on save.
                host.extra_options.append((key, value))
                continue
            seen.add(k)
            v = _unquote(v)
            if k == "hostname":
                host.hostname = v
            elif k == "user":
                host.user = v
            elif k == "port":
                host.port = _to_int(v)
                if host.port is None:
                    host.extra_options.append((key, value))
            elif k == "proxyjump":
                host.proxy_jump = v
            elif k == "proxycommand":
                host.proxy_command = value.strip()
            elif k == "forwardagent":
                host.forward_agent = parse_bool(v)
            elif k == "forwardx11":
                host.forward_x11 = parse_bool(v)
            elif k == "compression":
                host.compression = parse_bool(v)
            elif k == "serveraliveinterval":
                host.server_alive_interval = _to_int(v)
            elif k == "serveralivecountmax":
                host.server_alive_count_max = _to_int(v)
            elif k == "stricthostkeychecking":
                host.strict_host_key_checking = v
            elif k == "preferredauthentications":
                host.preferred_authentications = v
            elif k == "identitiesonly":
                host.identities_only = parse_bool(v)
            elif k == "addkeystoagent":
                host.add_keys_to_agent = v
        return host


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1]
    return v


def _to_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None
