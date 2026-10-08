"""Qt bridge between a :class:`TerminalBackend` and the terminal widget.

* Converts backend callbacks (worker threads) into Qt signals (GUI thread).
* Owns reconnection: manual (``reconnect()``) and automatic with back-off.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from ssh_terminal.errors import BackendError, FriendlyError
from ssh_terminal.models.connection import ConnectionSpec, SessionState
from ssh_terminal.terminal.backends.base import CloseInfo, TerminalBackend

log = logging.getLogger(__name__)

BackendFactory = Callable[[], TerminalBackend]


class TerminalSession(QObject):
    data_received = Signal(bytes)
    state_changed = Signal(object, str)  # SessionState, message
    closed = Signal(object)  # CloseInfo

    def __init__(
        self,
        spec: ConnectionSpec,
        factory: BackendFactory,
        auto_reconnect: bool = False,
        max_attempts: int = 5,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self._factory = factory
        self.backend: TerminalBackend | None = None
        self.state = SessionState.IDLE
        self.state_message = ""
        self.auto_reconnect = auto_reconnect
        self.max_attempts = max_attempts
        self._attempt = 0
        self._size = (80, 24)
        self._generation = 0  # ignore late callbacks from a replaced backend
        self.last_close: CloseInfo | None = None
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._start_backend)
        # Cross-thread delivery: these private signals are emitted from worker
        # threads and are queued to this object's (GUI) thread.
        self._data_sig.connect(self._on_data)
        self._state_sig.connect(self._on_state)
        self._closed_sig.connect(self._on_closed)

    _data_sig = Signal(int, bytes)
    _state_sig = Signal(int, object, str)
    _closed_sig = Signal(int, object)

    # ------------------------------------------------------------------ #
    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def is_connected(self) -> bool:
        return self.state is SessionState.CONNECTED

    @property
    def is_ssh(self) -> bool:
        return self.backend is not None and self.backend.kind == "ssh"

    def start(self, cols: int, rows: int) -> None:
        self._size = (cols, rows)
        self._attempt = 0
        self._start_backend()

    def reconnect(self) -> None:
        self._retry_timer.stop()
        self._attempt = 0
        self._close_backend()
        self._start_backend()

    def _start_backend(self) -> None:
        self._generation += 1
        gen = self._generation
        try:
            backend = self._factory()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            from ssh_terminal.errors import describe_exception

            self._fail(describe_exception(exc))
            return
        def safe(signal):  # noqa: ANN001, ANN202 - the tab may be closed while the worker still reports
            def emit(*args) -> None:  # noqa: ANN002
                try:
                    signal.emit(gen, *args)
                except RuntimeError:  # "Signal source has been deleted"
                    pass
            return emit

        backend.set_callbacks(safe(self._data_sig), safe(self._state_sig), safe(self._closed_sig))
        self.backend = backend
        try:
            backend.start(*self._size)
        except BackendError as exc:
            self._fail(FriendlyError("Could not start terminal", str(exc)))
        except OSError as exc:
            self._fail(FriendlyError("Could not start terminal", str(exc)))

    def _fail(self, error: FriendlyError) -> None:
        self._on_state(self._generation, SessionState.FAILED, error.title)
        self._on_closed(self._generation, CloseInfo(error.title, error=error))

    # ------------------------------------------------------------------ #
    def _on_data(self, gen: int, data: bytes) -> None:
        if gen == self._generation:
            self.data_received.emit(data)

    def _on_state(self, gen: int, state: SessionState, message: str) -> None:
        if gen != self._generation:
            return
        self.state = state
        self.state_message = message
        if state is SessionState.CONNECTED:
            self._attempt = 0
        self.state_changed.emit(state, message)

    def _on_closed(self, gen: int, info: CloseInfo) -> None:
        if gen != self._generation:
            return
        self.last_close = info
        if self.state not in (SessionState.FAILED, SessionState.DISCONNECTED):
            self.state = SessionState.DISCONNECTED
            self.state_changed.emit(self.state, info.reason)
        self.closed.emit(info)
        if info.unexpected and self.auto_reconnect and self._attempt < self.max_attempts:
            self._attempt += 1
            delay = min(30, 2 ** (self._attempt - 1)) * 1000
            log.info("Auto-reconnect %s in %ss (attempt %d)", self.title, delay // 1000, self._attempt)
            self._retry_timer.start(delay)

    # ------------------------------------------------------------------ #
    def write(self, data: bytes) -> None:
        if self.backend is not None and self.state is SessionState.CONNECTED:
            self.backend.write(data)

    def resize(self, cols: int, rows: int) -> None:
        if (cols, rows) == self._size:
            return
        self._size = (cols, rows)
        if self.backend is not None:
            self.backend.resize(cols, rows)

    def _close_backend(self) -> None:
        if self.backend is not None:
            backend, self.backend = self.backend, None
            self._generation += 1
            backend.close()

    def close(self) -> None:
        self._retry_timer.stop()
        self._close_backend()
        self.state = SessionState.DISCONNECTED
