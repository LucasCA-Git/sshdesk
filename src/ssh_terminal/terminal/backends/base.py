"""Transport-agnostic terminal backend interface.

The visual terminal never knows whether bytes come from an SSH channel, a
Unix PTY or a Windows ConPTY. New transports (serial, telnet, ``docker exec``,
``kubectl exec``, WSL...) only need to implement this class.

Backends run their I/O in background threads and report through plain
callbacks; :class:`ssh_terminal.terminal.terminal_session.TerminalSession`
turns them into Qt signals delivered on the GUI thread.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from ssh_terminal.errors import FriendlyError
from ssh_terminal.models.connection import SessionState

log = logging.getLogger(__name__)


@dataclass
class CloseInfo:
    reason: str
    unexpected: bool = False  # connection lost (eligible for auto-reconnect)
    error: FriendlyError | None = None
    exit_status: int | None = None


DataCallback = Callable[[bytes], None]
StateCallback = Callable[[SessionState, str], None]
ClosedCallback = Callable[[CloseInfo], None]


class TerminalBackend(ABC):
    """A bidirectional byte stream attached to a pseudo terminal."""

    kind = "generic"

    def __init__(self) -> None:
        self._on_data: DataCallback = lambda _d: None
        self._on_state: StateCallback = lambda _s, _m: None
        self._on_closed: ClosedCallback = lambda _i: None
        self._closed_reported = False
        self.user_closed = False

    def set_callbacks(self, on_data: DataCallback, on_state: StateCallback, on_closed: ClosedCallback) -> None:
        self._on_data = on_data
        self._on_state = on_state
        self._on_closed = on_closed

    # -- helpers for subclasses ------------------------------------------- #
    def emit_data(self, data: bytes) -> None:
        if data:
            self._on_data(data)

    def emit_state(self, state: SessionState, message: str = "") -> None:
        self._on_state(state, message)

    def emit_closed(self, info: CloseInfo) -> None:
        if self._closed_reported:
            return
        self._closed_reported = True
        if self.user_closed:
            info.unexpected = False
        log.info("Session closed (%s): %s", self.description, info.reason)
        self._on_closed(info)

    # -- interface ----------------------------------------------------------- #
    @property
    @abstractmethod
    def description(self) -> str:
        """Short human description, e.g. ``lucas@10.0.0.10:22`` or ``bash``."""

    @abstractmethod
    def start(self, cols: int, rows: int) -> None:
        """Start asynchronously. Must not block the GUI thread."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Send user input."""

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        """Propagate the terminal size (SIGWINCH / window-change)."""

    @abstractmethod
    def close(self) -> None:
        """Terminate the session (user initiated)."""

    @property
    def encoding(self) -> str:
        return "UTF-8"
