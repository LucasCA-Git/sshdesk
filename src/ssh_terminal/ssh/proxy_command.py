"""Cross-platform ``ProxyCommand`` socket.

``paramiko.ProxyCommand`` uses ``select()`` on a pipe, which only works on
POSIX (Windows ``select`` accepts sockets only). This implementation reads the
child's stdout in a thread, so it works on Windows and Linux, and starts the
process without a console window on Windows.
"""

from __future__ import annotations

import os
import queue
import shlex
import subprocess
import threading
import time

from ssh_terminal.utils.platform import is_windows


class ProxyCommandSocket:
    """Minimal socket-like object accepted by ``paramiko.Transport``."""

    def __init__(self, command: str) -> None:
        argv = shlex.split(command, posix=not is_windows())
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if is_windows() else 0
        self.process = subprocess.Popen(  # noqa: S603 - user-configured ProxyCommand
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, creationflags=flags
        )
        self._queue: queue.Queue[bytes] = queue.Queue()
        self._buffer = b""
        self._timeout: float | None = None
        self._closed = False
        threading.Thread(target=self._reader, name="proxycommand", daemon=True).start()

    def _reader(self) -> None:
        assert self.process.stdout is not None
        while True:
            try:
                # bufsize=0 gives a raw FileIO: os.read returns as soon as data arrives.
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except (OSError, ValueError):
                chunk = b""
            self._queue.put(chunk)
            if not chunk:
                return

    def send(self, data: bytes) -> int:
        if self._closed or self.process.stdin is None:
            raise OSError("ProxyCommand closed")
        self.process.stdin.write(data)
        self.process.stdin.flush()
        return len(data)

    def sendall(self, data: bytes) -> None:
        self.send(data)

    def recv(self, size: int) -> bytes:
        if not self._buffer:
            deadline = None if self._timeout is None else time.monotonic() + self._timeout
            try:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                self._buffer = self._queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("ProxyCommand read timed out") from exc
            if not self._buffer:
                self._queue.put(b"")  # keep EOF sticky
                return b""
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        self.process.terminate()

    @property
    def closed(self) -> bool:
        return self._closed
