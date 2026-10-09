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
import time

from ssh_terminal.errors import BackendError
from ssh_terminal.models.connection import SessionState
from ssh_terminal.terminal.backends.base import CloseInfo, TerminalBackend
from ssh_terminal.utils.platform import describe_exit_status

log = logging.getLogger(__name__)

QUICK_EXIT_SECONDS = 3.0  # a shell that dies this fast without output never really started


class WinPtyBackend(TerminalBackend):
    kind = "local"

    def __init__(self, argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        super().__init__()
        self.argv = argv
        self.cwd = cwd or os.path.expanduser("~")
        self.env = env
        self.proc = None
        self._lock = threading.Lock()
        self._dims = (24, 80)
        self._fallback_tried = False

    @property
    def description(self) -> str:
        return os.path.basename(self.argv[0]) if self.argv else "shell"

    def _spawn(self, legacy: bool = False):  # noqa: ANN202 - winpty.PtyProcess
        try:
            from winpty import PtyProcess
        except ImportError as exc:
            raise BackendError(
                "Local terminals on Windows require the 'pywinpty' package (pip install pywinpty)."
            ) from exc
        env = dict(self.env or os.environ)
        env.setdefault("TERM_PROGRAM", "SSHDesk")
        kwargs = {}
        if legacy:  # the old WinPTY agent instead of ConPTY
            from winpty import Backend

            kwargs["backend"] = Backend.WinPTY
        try:
            return PtyProcess.spawn(self.argv, cwd=self.cwd, env=env, dimensions=self._dims, **kwargs)
        except Exception as exc:  # noqa: BLE001 - winpty raises generic errors
            raise BackendError(f"Could not start {self.description}: {exc}") from exc

    def start(self, cols: int, rows: int) -> None:
        self._dims = (max(rows, 2), max(cols, 10))
        self.emit_state(SessionState.CONNECTING, f"Starting {self.description}")
        self.proc = self._spawn()
        self.emit_state(SessionState.CONNECTED, self.description)
        threading.Thread(target=self._read_loop, name="conpty-reader", daemon=True).start()

    def _pump(self, proc) -> bool:  # noqa: ANN001
        """Copy output until the process ends. Returns True if anything was printed."""
        got_output = False
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
            got_output = True
            self.emit_data(data)
        return got_output

    def _read_loop(self) -> None:
        while True:
            proc = self.proc
            assert proc is not None
            started = time.monotonic()
            got_output = self._pump(proc)
            status = getattr(proc, "exitstatus", None)
            quick = time.monotonic() - started < QUICK_EXIT_SECONDS and not got_output
            if self.user_closed or not quick or status in (0, None) or self._fallback_tried:
                break
            # ConPTY killed the shell before it printed anything (seen as 0xC000013A on some
            # machines): try once more with the WinPTY agent.
            self._fallback_tried = True
            log.warning("%s exited immediately (%s); retrying with WinPTY", self.description, describe_exit_status(status))
            try:
                new = self._spawn(legacy=True)
            except (BackendError, ImportError, AttributeError, TypeError) as exc:
                log.info("WinPTY fallback unavailable: %s", exc)
                break
            with self._lock:
                self.proc = new
            if self.user_closed:  # tab closed while the fallback was starting
                self.close()
                break
        self.emit_state(SessionState.DISCONNECTED, "Process exited")
        if self.user_closed:
            self.emit_closed(CloseInfo("Closed by user"))
        else:
            message = describe_exit_status(status)
            if quick and status not in (0, None):
                message += f" — {self.description} closed right after starting; try another shell from the Local Terminal menu"
            self.emit_closed(CloseInfo(message, exit_status=status))

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
        self._dims = (max(rows, 2), max(cols, 10))
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
