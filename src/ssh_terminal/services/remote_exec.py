"""Run commands on a server over an existing SSH connection.

One :class:`RemoteRunner` per server panel: short commands each open a session
channel on the same SSH transport (no new login), and long streams (``docker logs
-f``, ``docker pull``) get a channel of their own that is closed when the reader
stops. Nothing is installed on the server: only standard commands are executed.

Commands are always built from fixed parts plus :func:`quote` (``shlex.quote``)
for anything that comes from a user or from the server.
"""

from __future__ import annotations

import codecs
import logging
import select
import shlex
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

quote = shlex.quote


class RemoteTimeout(TimeoutError):
    """The command did not finish in time (the channel was closed)."""


@dataclass
class CommandResult:
    status: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.status == 0


class RemoteRunner:
    """Executes commands through one SSH connection; optionally through ``sudo``."""

    def __init__(self, connection, timeout: float = 30.0) -> None:  # noqa: ANN001 - SSHConnection
        self.connection = connection
        self.timeout = timeout
        self.use_sudo = False
        self._sudo_password: str | None = None  # memory only, for this panel's lifetime
        self._channels: set[object] = set()
        self._lock = threading.Lock()

    # -- sudo --------------------------------------------------------------- #
    def enable_sudo(self, password: str | None) -> None:
        """Run the next commands as ``sudo``. ``None`` = passwordless sudo (``-n``)."""
        self.use_sudo = True
        self._sudo_password = password

    def disable_sudo(self) -> None:
        self.use_sudo = False
        self._sudo_password = None

    def wrap(self, command: str, sudo: tuple[bool, str | None] | None = None) -> str:
        use_sudo, password = sudo if sudo is not None else (self.use_sudo, self._sudo_password)
        if not use_sudo:
            return command
        mode = "-S -p ''" if password is not None else "-n"
        return f"sudo {mode} sh -c {quote(command)}"

    # -- execution ---------------------------------------------------------- #
    def _open(self, command: str, timeout: float, keep_stdin: bool = False):  # noqa: ANN202 - paramiko.Channel
        sudo = (self.use_sudo, self._sudo_password)  # read once: a toggle mid-call can't misroute the password
        chan = self.connection.transport.open_session(timeout=15)
        chan.settimeout(timeout)
        chan.exec_command(self.wrap(command, sudo))
        if sudo[0] and sudo[1] is not None:
            chan.sendall((sudo[1] + "\n").encode())
        if not keep_stdin:
            chan.shutdown_write()  # no stdin: a wrong sudo password fails at once instead of waiting for a retry
        with self._lock:
            self._channels.add(chan)
        return chan

    def _forget(self, chan) -> None:  # noqa: ANN001
        with self._lock:
            self._channels.discard(chan)
        try:
            chan.close()
        except Exception:  # noqa: BLE001 - closing a dead channel
            pass

    @staticmethod
    def _finished(chan) -> bool:  # noqa: ANN001
        """All output read: OpenSSH may send the exit status before the last data, so also wait for EOF."""
        if chan.recv_ready() or chan.recv_stderr_ready():
            return False
        return chan.exit_status_ready() and (chan.eof_received or chan.closed)

    def run(self, command: str, timeout: float | None = None) -> CommandResult:
        """Run ``command`` and wait for it; stdout and stderr are read together (no deadlock)."""
        limit = timeout or self.timeout
        chan = self._open(command, limit)
        out, err = bytearray(), bytearray()
        deadline = time.monotonic() + limit
        try:
            while True:
                if chan.recv_ready():
                    out.extend(chan.recv(65536))
                    continue
                if chan.recv_stderr_ready():
                    err.extend(chan.recv_stderr(65536))
                    continue
                if self._finished(chan):
                    break
                if time.monotonic() > deadline:
                    raise RemoteTimeout(f"Command timed out after {int(limit)} s")
                select.select([chan], [], [], 0.1)
            status = chan.recv_exit_status()
        except RemoteTimeout:
            raise
        except TimeoutError as exc:  # paramiko's socket timeout
            raise RemoteTimeout(f"Command timed out after {int(limit)} s") from exc
        finally:
            self._forget(chan)
        return CommandResult(status, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    def stream(self, command: str, on_text: Callable[[str], None], stop: threading.Event) -> CommandResult:
        """Run a long command, passing output (stdout and stderr, in order received) to ``on_text``.

        Returns when the command ends or ``stop`` is set (the channel is closed then).
        ``err`` in the result holds the last stderr text, to explain failures.
        """
        # Without a terminal, closing the channel does not stop the remote process (e.g. `docker logs -f`
        # would run forever). The watcher below kills it as soon as our side of the channel goes away.
        # fd 3 keeps the channel's stdin: background jobs of a non-interactive shell get /dev/null instead.
        watched = (f"exec 3<&0; {command} & p=$!; (cat <&3 >/dev/null; kill $p 2>/dev/null) >/dev/null 2>&1 & "
                   f"wait $p")
        chan = self._open(f"sh -c {quote(watched)}", 0.5, keep_stdin=True)
        last_err = ""
        out_dec = codecs.getincrementaldecoder("utf-8")("replace")  # a chunk may end mid-character
        err_dec = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while not stop.is_set():
                got = False
                if chan.recv_ready():
                    data = chan.recv(65536)
                    if data:
                        on_text(out_dec.decode(data))
                        got = True
                if chan.recv_stderr_ready():
                    data = chan.recv_stderr(65536)
                    if data:
                        text = err_dec.decode(data)
                        last_err = (last_err + text)[-4000:]
                        on_text(text)
                        got = True
                if not got:
                    if self._finished(chan):
                        return CommandResult(chan.recv_exit_status(), "", last_err)
                    select.select([chan], [], [], 0.2)
            return CommandResult(-1, "", last_err)  # stopped by the user
        finally:
            self._forget(chan)

    def close(self) -> None:
        """Stop every running command (closing a channel ends the remote process's I/O)."""
        with self._lock:
            channels = list(self._channels)
            self._channels.clear()
        for chan in channels:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass
