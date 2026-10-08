"""Plain data models (no Qt, no I/O)."""

from ssh_terminal.models.app_settings import AppSettings
from ssh_terminal.models.connection import ConnectionKind, ConnectionSpec, PortForwardSpec, SessionState
from ssh_terminal.models.ssh_host import SSHHost

__all__ = ["AppSettings", "ConnectionKind", "ConnectionSpec", "PortForwardSpec", "SSHHost", "SessionState"]
