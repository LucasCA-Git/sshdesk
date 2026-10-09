"""Local shell on Linux/macOS through a real PTY.

``pty.fork()`` is used (instead of ``subprocess`` + ``os.openpty``) because
the child must become a session leader *and* acquire the PTY as its
controlling terminal; without that, Ctrl+C/Ctrl+Z job control does not work.
The child immediately ``exec``s the shell, which keeps forking from a
multi-threaded process safe.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import signal
import struct
import sys
import threading
import warnings

from ssh_terminal.errors import BackendError, FriendlyError
from ssh_terminal.models.connection import SessionState
from ssh_terminal.terminal.backends.base import CloseInfo, TerminalBackend

log = logging.getLogger(__name__)


class UnixPtyBackend(TerminalBackend):
    kind = "local"

    def __init__(self, argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None, term_type: str = "xterm-256color") -> None:
        super().__init__()
        self.argv = argv
        self.cwd = cwd or os.path.expanduser("~")
        self.env = env
        self.term_type = term_type
        self.pid: int | None = None
        self.fd: int | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return os.path.basename(self.argv[0]) if self.argv else "shell"

    def start(self, cols: int, rows: int) -> None:
        import pty  # POSIX only

        env = dict(self.env or os.environ)
        env.update({"TERM": self.term_type, "COLORTERM": "truecolor", "TERM_PROGRAM": "SSHDesk"})
        # macOS has no C.UTF-8 locale; apps started from Finder get no LANG at all
        env.setdefault("LANG", "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8")
        self.emit_state(SessionState.CONNECTING, f"Starting {self.description}")
        try:
            with warnings.catch_warnings():
                # Python 3.12+ warns about fork() in multi-threaded processes;
                # the child execs immediately, which is the supported pattern.
                warnings.simplefilter("ignore", DeprecationWarning)
                pid, fd = pty.fork()
        except OSError as exc:
            raise BackendError(f"Could not create a pseudo terminal: {exc}") from exc
        if pid == 0:  # child
            try:
                os.chdir(self.cwd)
            except OSError:
                pass
            try:
                os.execvpe(self.argv[0], self.argv, env)
            except OSError as exc:
                os.write(2, f"SSHDesk: cannot execute {self.argv[0]}: {exc}\r\n".encode())
            os._exit(127)
        self.pid, self.fd = pid, fd
        self.resize(cols, rows)
        self.emit_state(SessionState.CONNECTED, self.description)
        threading.Thread(target=self._read_loop, name="pty-reader", daemon=True).start()

    def _read_loop(self) -> None:
        fd = self.fd
        assert fd is not None
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:  # child exited (slave side closed)
                    break
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    continue
                log.info("PTY read error: %s", exc)
                break
            if not data:
                break
            self.emit_data(data)
        status = self._reap()
        self._close_fd()
        self.emit_state(SessionState.DISCONNECTED, "Process exited")
        if self.user_closed:
            self.emit_closed(CloseInfo("Closed by user"))
        elif status == 127:
            self.emit_closed(
                CloseInfo(
                    "Shell could not be started",
                    error=FriendlyError("Shell could not be started", f"Could not execute {' '.join(self.argv)}"),
                    exit_status=status,
                )
            )
        else:
            self.emit_closed(CloseInfo(f"Process exited (status {status})", exit_status=status))

    def _reap(self) -> int | None:
        if self.pid is None:
            return None
        for _ in range(20):
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if pid:
                return os.waitstatus_to_exitcode(status)
            self._stop.wait(0.05)
        return None

    def _close_fd(self) -> None:
        with self._lock:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None

    def write(self, data: bytes) -> None:
        with self._lock:
            fd = self.fd
            if fd is None:
                return
            view = memoryview(data)
            while view:
                try:
                    written = os.write(fd, view)
                except InterruptedError:
                    continue
                except OSError as exc:
                    log.info("PTY write failed: %s", exc)
                    return
                view = view[written:]

    def resize(self, cols: int, rows: int) -> None:
        import fcntl
        import termios

        fd = self.fd
        if fd is None:
            return
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError as exc:
            log.debug("TIOCSWINSZ failed: %s", exc)

    def close(self) -> None:
        self.user_closed = True
        if self.pid is not None:
            for sig in (signal.SIGHUP, signal.SIGTERM):
                try:
                    os.kill(self.pid, sig)
                except ProcessLookupError:
                    break
                except OSError as exc:
                    log.debug("kill failed: %s", exc)
                    break
        self._stop.set()
