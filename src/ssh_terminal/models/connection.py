"""What a terminal tab is connected to.

A :class:`ConnectionSpec` is a small serialisable description used to open,
reopen, duplicate (split) and restore tabs. It references SSH hosts by alias,
so the SSH config stays the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ConnectionKind(StrEnum):
    SSH = "ssh"
    LOCAL = "local"


class SessionState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"


@dataclass
class ConnectionSpec:
    kind: ConnectionKind
    alias: str = ""  # SSH alias (kind=SSH)
    shell: str = ""  # ShellProfile name (kind=LOCAL); empty = default
    adhoc: dict[str, Any] = field(default_factory=dict)  # user/host/port for "ssh user@host" not in config
    theme: str = ""  # per-terminal color scheme ("" = app setting)

    @property
    def title(self) -> str:
        if self.kind is ConnectionKind.SSH:
            return self.alias or self.adhoc.get("hostname", "ssh")
        return self.shell or "Local"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "alias": self.alias, "shell": self.shell, "adhoc": dict(self.adhoc), "theme": self.theme}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectionSpec:
        return cls(
            kind=ConnectionKind(data.get("kind", "local")),
            alias=str(data.get("alias", "")),
            shell=str(data.get("shell", "")),
            adhoc=dict(data.get("adhoc") or {}),
            theme=str(data.get("theme", "")),
        )

    @classmethod
    def ssh(cls, alias: str) -> ConnectionSpec:
        return cls(ConnectionKind.SSH, alias=alias)

    @classmethod
    def local(cls, shell: str = "") -> ConnectionSpec:
        return cls(ConnectionKind.LOCAL, shell=shell)


@dataclass
class PortForwardSpec:
    """A port forward. ``kind`` is ``local`` (-L), ``remote`` (-R) or ``dynamic`` (-D)."""

    kind: str
    bind_address: str
    bind_port: int
    target_host: str = ""
    target_port: int = 0

    def describe(self) -> str:
        if self.kind == "dynamic":
            return f"SOCKS {self.bind_address}:{self.bind_port}"
        arrow = "→" if self.kind == "local" else "←"
        return f"{self.bind_address}:{self.bind_port} {arrow} {self.target_host}:{self.target_port}"

    @classmethod
    def parse(cls, kind: str, spec: str) -> PortForwardSpec:
        """Parse ``LocalForward``/``RemoteForward``/``DynamicForward`` values.

        Accepted forms: ``[bind:]port host:hostport`` (local/remote; also
        ``host/port`` IPv6 style) and ``[bind:]port`` (dynamic).
        """
        parts = spec.split()
        if not parts:
            raise ValueError("empty forward specification")
        bind_address, bind_port = _split_bind(parts[0])
        if kind == "dynamic":
            return cls("dynamic", bind_address, bind_port)
        if len(parts) < 2:
            raise ValueError(f"missing target in forward '{spec}'")
        target = parts[1]
        if target.startswith("["):
            host, _, port = target[1:].partition("]:")
        elif "/" in target:
            host, _, port = target.rpartition("/")
        else:
            host, _, port = target.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"invalid target in forward '{spec}'")
        return cls(kind, bind_address, bind_port, host, int(port))


def _split_bind(token: str) -> tuple[str, int]:
    if token.startswith("["):
        host, _, port = token[1:].partition("]:")
    elif token.count(":") == 1 or "/" in token:
        sep = "/" if "/" in token else ":"
        host, _, port = token.rpartition(sep)
    else:
        host, port = "", token
    if not port.isdigit():
        raise ValueError(f"invalid port '{token}'")
    if host in ("", "localhost"):
        host = "127.0.0.1"
    elif host == "*":
        host = "0.0.0.0"
    return host, int(port)
