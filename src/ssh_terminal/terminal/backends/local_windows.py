"""Local shell on Windows through ConPTY (via ``pywinpty``).

ConPTY (Windows 10 1809+) is the only way to get a real interactive console
(PowerShell line editing, colours, Ctrl+C, resize) from a GUI application.
``pywinpty`` wraps it and exposes a ptyprocess-like API:
``PtyProcess.spawn(argv, cwd, env, dimensions=(rows, cols))``, ``read()``,
``write()``, ``setwinsize(rows, cols)``, ``isalive()``, ``terminate()``.
"""

from __future__ import annotations

import logging
import os
import threading

from ssh_terminal.errors import BackendError
from ssh_terminal.models.connection import SessionState
from ssh_terminal.terminal.backends.base import CloseInfo, TerminalBackend

log = logging.getLogger(__name__)


class WinPtyBackend(TerminalBackend):
    kind = "local"

    def __init__(self, argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        super().__init__()
        self.argv = argv
        self.cwd = cwd or os.path.expanduser("~")
        self.env = env
        self.proc = None
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return os.path.basename(self.argv[0]) if self.argv else "shell"

    def start(self, cols: int, rows: int) -> None:
        try:
            from winpty import PtyProcess
        except ImportError as exc:
            raise BackendError(
                "Local terminals on Windows require the 'pywinpty' package (pip install pywinpty)."
            ) from exc
        env = dict(self.env or os.environ)
        env.setdefault("TERM_PROGRAM", "SSHDesk")
        self.emit_state(SessionState.CONNECTING, f"Starting {self.description}")
        try:
            self.proc = PtyProcess.spawn(self.argv, cwd=self.cwd, env=env, dimensions=(rows, cols))
        except Exception as exc:  # noqa: BLE001 - winpty raises generic errors
            raise BackendError(f"Could not start {self.description}: {exc}") from exc
        self.emit_state(SessionState.CONNECTED, self.description)
        threading.Thread(target=self._read_loop, name="conpty-reader", daemon=True).start()

    def _read_loop(self) -> None:
        proc = self.proc
        assert proc is not None
        while True:
            try:
                data = proc.read(65536)
            except EOFError:
                break
            except Exception as exc:  # noqa: BLE001 - winpty raises generic errors on close
                log.info("ConPTY read ended: %s", exc)
                break
            if not data:
                if not proc.isalive():
                    break
                continue
            if isinstance(data, str):
                data = data.encode("utf-8", errors="replace")
            self.emit_data(data)
        status = getattr(proc, "exitstatus", None)
        self.emit_state(SessionState.DISCONNECTED, "Process exited")
        if self.user_closed:
            self.emit_closed(CloseInfo("Closed by user"))
        else:
            self.emit_closed(CloseInfo(f"Process exited (status {status})", exit_status=status))

    def write(self, data: bytes) -> None:
        proc = self.proc
        if proc is None:
            return
        with self._lock:
            try:
                proc.write(data.decode("utf-8", errors="replace"))
            except Exception as exc:  # noqa: BLE001
                log.info("ConPTY write failed: %s", exc)

    def resize(self, cols: int, rows: int) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            proc.setwinsize(rows, cols)
        except Exception as exc:  # noqa: BLE001
            log.debug("ConPTY resize failed: %s", exc)

    def close(self) -> None:
        self.user_closed = True
        proc = self.proc
        if proc is None:
            return
        try:
            proc.terminate(force=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("ConPTY terminate: %s", exc)
        try:
            proc.close(force=True)  # releases the ConPTY handle and its socket pair
        except Exception as exc:  # noqa: BLE001
            log.debug("ConPTY close: %s", exc)
