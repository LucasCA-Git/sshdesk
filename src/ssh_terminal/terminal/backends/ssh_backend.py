"""SSH backend: a Paramiko shell channel with a PTY."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

import paramiko

from ssh_terminal.errors import describe_exception
from ssh_terminal.models.connection import PortForwardSpec, SessionState
from ssh_terminal.ssh.forwarding import ForwardingManager
from ssh_terminal.ssh.resolver import ResolvedConfig
from ssh_terminal.ssh.ssh_session import SSHConnection, SSHConnector
from ssh_terminal.terminal.backends.base import CloseInfo, TerminalBackend

log = logging.getLogger(__name__)


class SSHBackend(TerminalBackend):
    kind = "ssh"

    def __init__(
        self,
        resolve: Callable[[], ResolvedConfig],
        connector_factory: Callable[[Callable[[str], None]], SSHConnector],
        term_type: str = "xterm-256color",
        apply_config_forwards: bool = True,
    ) -> None:
        super().__init__()
        self._resolve = resolve
        self._connector_factory = connector_factory
        self.term_type = term_type
        self.apply_config_forwards = apply_config_forwards
        self.resolved: ResolvedConfig | None = None
        self.connection: SSHConnection | None = None
        self.channel: paramiko.Channel | None = None
        self.forwarding: ForwardingManager | None = None
        self._connector: SSHConnector | None = None
        self._size = (80, 24)
        # Input is sent by a dedicated thread: Channel.sendall() blocks while
        # the remote window is full (e.g. a big paste), which must never
        # freeze the GUI thread.
        self._outbox: queue.Queue[bytes | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    @property
    def description(self) -> str:
        return self.resolved.endpoint if self.resolved else "ssh"

    def start(self, cols: int, rows: int) -> None:
        self._size = (cols, rows)
        self._thread = threading.Thread(target=self._run, name="ssh-session", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        self.emit_state(SessionState.CONNECTING, "Resolving configuration")
        try:
            self.resolved = self._resolve()
            self._connector = self._connector_factory(lambda msg: self.emit_state(SessionState.CONNECTING, msg))
            self.connection = self._connector.connect(self.resolved)
            if self.user_closed:
                self.connection.close()
                return
            cols, rows = self._size
            self.channel = self.connection.open_shell(self.term_type, cols, rows)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            if self.user_closed:
                return
            friendly = describe_exception(exc, self.resolved.hostname if self.resolved else "")
            log.warning("SSH connection failed: %s", friendly.message.splitlines()[0])
            self.emit_state(SessionState.FAILED, friendly.title)
            if self.connection is not None:
                self.connection.close()
            self.emit_closed(CloseInfo(friendly.title, unexpected=False, error=friendly))
            return

        threading.Thread(target=self._write_loop, name="ssh-writer", daemon=True).start()
        self.forwarding = ForwardingManager(self.connection.transport)
        if self.apply_config_forwards:
            self._start_config_forwards()
        self.emit_state(SessionState.CONNECTED, self.description)
        self._read_loop()
        self._outbox.put(None)

    def _start_config_forwards(self) -> None:
        assert self.resolved is not None and self.forwarding is not None
        for kind, specs in (
            ("local", self.resolved.local_forwards),
            ("remote", self.resolved.remote_forwards),
            ("dynamic", self.resolved.dynamic_forwards),
        ):
            for raw in specs:
                try:
                    self.forwarding.start(PortForwardSpec.parse(kind, raw))
                except (ValueError, OSError, paramiko.SSHException) as exc:
                    msg = f"\r\n\x1b[33m[SSHDesk] Could not start {kind} forward '{raw}': {exc}\x1b[0m\r\n"
                    self.emit_data(msg.encode())

    def _read_loop(self) -> None:
        assert self.channel is not None and self.connection is not None
        chan = self.channel
        while True:
            try:
                data = chan.recv(65536)
            except TimeoutError:
                continue
            except (OSError, EOFError, paramiko.SSHException) as exc:
                log.info("SSH read error: %s", exc)
                data = b""
            if not data:
                break
            self.emit_data(data)

        transport_alive = self.connection.transport.is_active()
        # exit_status_ready() is also True for a channel closed without an
        # exit-status message; paramiko then leaves the -1 sentinel.
        status = chan.exit_status if chan.status_event.is_set() and chan.exit_status != -1 else None
        if self.forwarding is not None:
            self.forwarding.stop_all()
        self.connection.close()
        if self.user_closed:
            self.emit_state(SessionState.DISCONNECTED, "Closed")
            self.emit_closed(CloseInfo("Closed by user"))
        elif status is not None:
            self.emit_state(SessionState.DISCONNECTED, "Session ended")
            self.emit_closed(CloseInfo(f"Session ended (exit status {status})", unexpected=False, exit_status=status))
        elif transport_alive:
            self.emit_state(SessionState.DISCONNECTED, "Channel closed")
            self.emit_closed(CloseInfo("Channel closed by the server", unexpected=True))
        else:
            self.emit_state(SessionState.DISCONNECTED, "Connection lost")
            self.emit_closed(CloseInfo("Connection lost", unexpected=True))

    # ------------------------------------------------------------------ #
    def write(self, data: bytes) -> None:
        chan = self.channel
        if chan is None or chan.closed:
            return
        self._outbox.put(data)

    def _write_loop(self) -> None:
        chan = self.channel
        while chan is not None:
            data = self._outbox.get()
            if data is None or chan.closed:
                return
            try:
                chan.sendall(data)
            except (OSError, EOFError, paramiko.SSHException) as exc:
                log.info("SSH write failed: %s", exc)
                return

    def resize(self, cols: int, rows: int) -> None:
        self._size = (cols, rows)
        chan = self.channel
        if chan is not None and not chan.closed:
            try:
                chan.resize_pty(width=cols, height=rows)
            except (OSError, EOFError, paramiko.SSHException) as exc:
                log.debug("resize_pty failed: %s", exc)

    def close(self) -> None:
        self.user_closed = True
        self._outbox.put(None)
        if self._connector is not None:
            self._connector.cancel()
        if self.channel is not None:
            try:
                self.channel.close()
            except (OSError, paramiko.SSHException):
                pass
        if self.connection is not None:
            if self.forwarding is not None:
                self.forwarding.stop_all()
            self.connection.close()
