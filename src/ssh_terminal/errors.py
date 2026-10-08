"""Custom exceptions and user-friendly error descriptions."""

from __future__ import annotations

import socket
import traceback
from dataclasses import dataclass, field


class SSHDeskError(Exception):
    """Base class for all application errors."""


class ConfigError(SSHDeskError):
    """Problem reading, parsing or writing the SSH configuration."""


class HostNotFoundError(ConfigError):
    def __init__(self, alias: str) -> None:
        super().__init__(f'Host "{alias}" was not found in the SSH configuration.')
        self.alias = alias


class DuplicateHostError(ConfigError):
    def __init__(self, alias: str) -> None:
        super().__init__(f'A host named "{alias}" already exists.')
        self.alias = alias


class ConnectionError_(SSHDeskError):
    """Connection level failure (named with a trailing underscore to avoid the builtin)."""


class HostKeyRejected(ConnectionError_):
    """The user (or StrictHostKeyChecking) refused an unknown host key."""


class HostKeyMismatch(ConnectionError_):
    def __init__(self, host: str, key_type: str, fingerprint: str, known_hosts: str) -> None:
        super().__init__(f"Host key for {host} has changed")
        self.host = host
        self.key_type = key_type
        self.fingerprint = fingerprint
        self.known_hosts = known_hosts


class AuthenticationFailed(ConnectionError_):
    def __init__(self, message: str, tried: list[str] | None = None, allowed: list[str] | None = None) -> None:
        super().__init__(message)
        self.tried = tried or []
        self.allowed = allowed or []


class ConnectionCancelled(ConnectionError_):
    """The user cancelled a prompt (password, passphrase, host key)."""


class BackendError(SSHDeskError):
    """A terminal backend (PTY/ConPTY) could not be started."""


@dataclass
class FriendlyError:
    """An error ready to be displayed: short title, explanation and technical details."""

    title: str
    message: str
    causes: list[str] = field(default_factory=list)
    details: str = ""

    def as_text(self) -> str:
        text = f"{self.title}\n\n{self.message}"
        if self.causes:
            text += "\n\nPossible causes:\n\n" + "\n".join(f"• {c}" for c in self.causes)
        return text


def describe_exception(exc: BaseException, context: str = "") -> FriendlyError:
    """Translate an exception into a message a human can act on."""
    # Imported lazily so this module stays importable without paramiko.
    import paramiko

    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
    where = f" to {context}" if context else ""

    if isinstance(exc, HostKeyMismatch):
        return FriendlyError(
            "Host key verification failed",
            f"WARNING: the host key for {exc.host} has CHANGED.\n"
            f"New {exc.key_type} key fingerprint: {exc.fingerprint}\n\n"
            "Someone could be eavesdropping on you (man-in-the-middle attack), "
            "or the server was reinstalled.",
            [
                "The server was reinstalled or its keys were regenerated",
                "The IP/hostname now points to a different machine",
                "A man-in-the-middle attack",
            ],
            f"If you trust the new key, remove the old one with:\n"
            f"    ssh-keygen -R {exc.host} -f \"{exc.known_hosts}\"\n\n{details}",
        )
    if isinstance(exc, HostKeyRejected):
        return FriendlyError("Host key not accepted", str(exc) or "The server's host key was not trusted.", [], details)
    if isinstance(exc, ConnectionCancelled):
        return FriendlyError("Connection cancelled", "The connection was cancelled.", [], details)
    if isinstance(exc, AuthenticationFailed) or isinstance(exc, paramiko.AuthenticationException):
        tried = getattr(exc, "tried", [])
        allowed = getattr(exc, "allowed", [])
        msg = "Authentication failed."
        if tried:
            msg += f"\n\nMethods tried: {', '.join(tried)}"
        if allowed:
            msg += f"\nServer accepts: {', '.join(allowed)}"
        return FriendlyError(
            "Authentication failed",
            msg,
            [
                "Invalid username",
                "SSH key rejected (public key not in authorized_keys)",
                "SSH agent unavailable or without keys",
                "Wrong password or passphrase",
                "Server does not accept this authentication method",
            ],
            details,
        )
    if isinstance(exc, socket.gaierror):
        return FriendlyError(
            "Host not found",
            f"Could not resolve the host name{where}.",
            ["Typo in HostName", "DNS unavailable / VPN disconnected", "Host only reachable through a ProxyJump"],
            details,
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return FriendlyError(
            "Connection failed",
            f"Connection timed out{where}.",
            ["Host is down or unreachable", "Firewall dropping packets", "Wrong port", "VPN required"],
            details,
        )
    if isinstance(exc, ConnectionRefusedError):
        return FriendlyError(
            "Connection refused",
            f"The server refused the connection{where}.",
            ["SSH server not running", "Wrong port", "Firewall rejecting the connection"],
            details,
        )
    if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        return FriendlyError(
            "Connection failed",
            f"Unable to connect{where}: {exc}",
            ["SSH server not running", "Wrong port", "Firewall rejecting the connection"],
            details,
        )
    if isinstance(exc, paramiko.ProxyCommandFailure):
        return FriendlyError("ProxyCommand failed", str(exc), ["ProxyCommand binary missing", "Jump host unreachable"], details)
    if isinstance(exc, paramiko.SSHException):
        text = str(exc)
        causes = []
        if "Error reading SSH protocol banner" in text:
            causes = ["The port is not an SSH server", "Server overloaded (MaxStartups)", "Proxy closed the stream"]
        elif "Unable to open channel" in text or "ChannelException" in type(exc).__name__:
            causes = ["The jump host is not allowed to forward to the target (AllowTcpForwarding)", "Target unreachable from the jump host"]
        return FriendlyError("SSH error", text or type(exc).__name__, causes, details)
    if isinstance(exc, OSError):
        return FriendlyError("Connection failed", f"{exc.strerror or exc}{where}.", [], details)
    if isinstance(exc, SSHDeskError):
        return FriendlyError("Error", str(exc), [], details)
    return FriendlyError("Unexpected error", f"{type(exc).__name__}: {exc}", [], details)
