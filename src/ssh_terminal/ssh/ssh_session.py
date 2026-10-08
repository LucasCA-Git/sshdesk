"""Establishing SSH connections with Paramiko.

Flow (always executed in a worker thread)::

    ResolvedConfig
        -> open socket (direct TCP | ProxyCommand | ProxyJump chain)
        -> Transport handshake (host key verification against known_hosts)
        -> user authentication (see ssh_auth)
        -> SSHConnection (shell channels, SFTP, port forwards)

ProxyJump is implemented natively: each hop is a full SSH connection and the
next hop is reached through a ``direct-tcpip`` channel of the previous one
(equivalent to ``ssh -J``), so no external ``ssh`` binary is required.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import paramiko

from ssh_terminal.errors import ConnectionError_, HostKeyMismatch, HostKeyRejected
from ssh_terminal.ssh.host_keys import HostKeyStatus, KnownHostsStore, host_id_for
from ssh_terminal.ssh.proxy_command import ProxyCommandSocket
from ssh_terminal.ssh.resolver import ResolvedConfig, expand_tokens, resolve_jump_spec
from ssh_terminal.ssh.ssh_auth import Authenticator, AuthOptions, AuthPrompter, CredentialStore
from ssh_terminal.utils.paths import expand_user_path

log = logging.getLogger(__name__)

MAX_JUMP_DEPTH = 8


@dataclass
class ConnectOptions:
    timeout: float = 15.0
    keepalive: int = 30
    use_agent: bool = True
    allow_keyring: bool = True
    default_strict: str = "ask"


ProgressCallback = Callable[[str], None]


class SSHConnection:
    """An authenticated SSH transport (plus the jump connections it depends on)."""

    def __init__(self, transport: paramiko.Transport, cfg: ResolvedConfig, jumps: list[SSHConnection] | None = None) -> None:
        self.transport = transport
        self.config = cfg
        self.jumps = jumps or []
        self._agent_handlers: list[object] = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def is_active(self) -> bool:
        return not self._closed and self.transport.is_active()

    def open_shell(self, term: str, cols: int, rows: int, forward_agent: bool | None = None, timeout: float = 15.0) -> paramiko.Channel:
        chan = self.transport.open_session(timeout=timeout)
        chan.get_pty(term=term, width=cols, height=rows)
        if forward_agent if forward_agent is not None else self.config.forward_agent:
            try:
                handler = paramiko.agent.AgentRequestHandler(chan)
                self._agent_handlers.append(handler)
            except (paramiko.SSHException, OSError) as exc:
                log.warning("Agent forwarding unavailable: %s", exc)
        chan.invoke_shell()
        return chan

    def exec_command(self, command: str, timeout: float = 15.0) -> tuple[int, str, str]:
        """Run a non-interactive command (used by diagnostics/tests)."""
        chan = self.transport.open_session(timeout=timeout)
        chan.settimeout(timeout)
        chan.exec_command(command)
        out = bytearray()
        err = bytearray()
        while True:
            data = chan.recv(65536)
            if not data:
                break
            out.extend(data)
        while chan.recv_stderr_ready():
            err.extend(chan.recv_stderr(65536))
        status = chan.recv_exit_status()
        chan.close()
        return status, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for handler in self._agent_handlers:
            close = getattr(handler, "close", None)
            if callable(close):
                close()
        try:
            self.transport.close()
        finally:
            for jump in reversed(self.jumps):
                jump.close()
        log.info("SSH connection closed: %s", self.config.endpoint)


class SSHConnector:
    """Creates :class:`SSHConnection` objects from resolved configurations."""

    def __init__(
        self,
        resolve: Callable[[str], ResolvedConfig],
        prompter: AuthPrompter,
        credentials: CredentialStore,
        options: ConnectOptions | None = None,
        password: str | None = None,
        progress: ProgressCallback | None = None,
        transport_factory: Callable[[object], paramiko.Transport] | None = None,
    ) -> None:
        self.resolve = resolve
        self.prompter = prompter
        self.credentials = credentials
        self.options = options or ConnectOptions()
        self.password = password
        self.progress = progress or (lambda _msg: None)
        self._transport_factory = transport_factory or paramiko.Transport
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------------ #
    def connect(self, cfg: ResolvedConfig, sock: object | None = None, _depth: int = 0) -> SSHConnection:
        if _depth > MAX_JUMP_DEPTH:
            raise ConnectionError_("Too many ProxyJump hops (loop in the SSH configuration?)")
        jumps: list[SSHConnection] = []
        timeout = float(cfg.connect_timeout or self.options.timeout)
        try:
            if sock is None:
                sock, jumps = self._open_socket(cfg, timeout, _depth)
            if self._cancel.is_set():
                raise ConnectionError_("Cancelled")
            transport = self._transport_factory(sock)
            try:
                connection = self._handshake(transport, cfg, timeout)
            except BaseException:
                transport.close()
                raise
            connection.jumps = jumps
            return connection
        except BaseException:
            for jump in reversed(jumps):
                jump.close()
            raise

    def _open_socket(self, cfg: ResolvedConfig, timeout: float, depth: int) -> tuple[object, list[SSHConnection]]:
        if cfg.proxy_jump:
            hops = [h.strip() for h in cfg.proxy_jump.split(",") if h.strip()]
            jumps: list[SSHConnection] = []
            previous: SSHConnection | None = None
            try:
                for hop in hops:
                    hop_cfg = self._resolve_hop(hop)
                    self.progress(f"Connecting to jump host {hop_cfg.alias} ({hop_cfg.hostname}:{hop_cfg.port})")
                    if previous is None:
                        conn = self.connect(hop_cfg, _depth=depth + 1)
                    else:
                        hop_cfg = replace(hop_cfg, proxy_jump=None, proxy_command=None)
                        chan = self._direct_tcpip(previous, hop_cfg.hostname, hop_cfg.port, timeout)
                        conn = self.connect(hop_cfg, sock=chan, _depth=depth + 1)
                    jumps.append(conn)
                    previous = conn
                assert previous is not None
                self.progress(f"Opening tunnel to {cfg.hostname}:{cfg.port}")
                return self._direct_tcpip(previous, cfg.hostname, cfg.port, timeout), jumps
            except BaseException:
                for jump in reversed(jumps):
                    jump.close()
                raise
        if cfg.proxy_command:
            command = expand_tokens(cfg.proxy_command, hostname=cfg.hostname, port=cfg.port, user=cfg.user, alias=cfg.alias)
            self.progress(f"Starting ProxyCommand for {cfg.alias}")
            return ProxyCommandSocket(command), []
        self.progress(f"Connecting to {cfg.hostname}:{cfg.port}")
        sock = socket.create_connection((cfg.hostname, cfg.port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock, []

    def _resolve_hop(self, spec: str) -> ResolvedConfig:
        user, host, port = resolve_jump_spec(spec)
        hop = self.resolve(host)
        if user:
            hop = replace(hop, user=user)
        if port:
            hop = replace(hop, port=port)
        return hop

    @staticmethod
    def _direct_tcpip(via: SSHConnection, host: str, port: int, timeout: float) -> paramiko.Channel:
        return via.transport.open_channel("direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=timeout)

    # ------------------------------------------------------------------ #
    def _known_hosts(self, cfg: ResolvedConfig) -> KnownHostsStore:
        user_files = [expand_user_path(p) for p in cfg.user_known_hosts_files] or None
        global_files = [expand_user_path(p) for p in cfg.global_known_hosts_files]
        if not global_files:
            global_files = [Path("/etc/ssh/ssh_known_hosts")]
        return KnownHostsStore(user_files or [], global_files)

    def _handshake(self, transport: paramiko.Transport, cfg: ResolvedConfig, timeout: float) -> SSHConnection:
        transport.use_compression(cfg.compression)
        known = self._known_hosts(cfg)
        # OpenSSH uses HostKeyAlias verbatim (no [host]:port decoration).
        host_id = cfg.host_key_alias or host_id_for(cfg.hostname, cfg.port)
        try:
            opts = transport.get_security_options()
            opts.key_types = known.preferred_algorithms(host_id, tuple(opts.key_types))
        except (AttributeError, ValueError) as exc:
            log.debug("Could not reorder host key algorithms: %s", exc)

        self.progress(f"SSH handshake with {cfg.hostname}")
        transport.start_client(timeout=timeout)
        self._verify_host_key(transport, cfg, known, host_id)

        self.progress(f"Authenticating as {cfg.user}")
        # Prompts (password, OTP) run while paramiko waits; its default 30 s
        # auth timeout would expire while the dialog is still open.
        transport.auth_timeout = 300
        auth = Authenticator(
            self.prompter,
            self.credentials,
            AuthOptions(use_agent=self.options.use_agent, allow_keyring=self.options.allow_keyring, password=self.password),
        )
        auth.authenticate(transport, cfg)
        interval = cfg.server_alive_interval or self.options.keepalive
        if interval:
            transport.set_keepalive(interval)
        log.info("SSH connection established: %s", cfg.endpoint)
        return SSHConnection(transport, cfg)

    def _verify_host_key(self, transport: paramiko.Transport, cfg: ResolvedConfig, known: KnownHostsStore, host_id: str) -> None:
        key = transport.get_remote_server_key()
        check = known.check(host_id, key)
        if check.status is HostKeyStatus.KNOWN:
            return
        if check.status is HostKeyStatus.MISMATCH:
            raise HostKeyMismatch(host_id, check.key_type, check.fingerprint, str(known.user_files[0]))

        # "ask" is OpenSSH's default; the app-level default (Settings > Security)
        # only replaces it, never an explicit yes/no from the SSH config.
        policy = (cfg.strict_host_key_checking or "ask").lower()
        if policy == "ask":
            policy = self.options.default_strict.lower()
        if policy in ("yes", "true"):
            raise HostKeyRejected(
                f"No matching host key for {host_id} in known_hosts and StrictHostKeyChecking is enabled."
            )
        if policy in ("no", "off", "false", "accept-new"):
            known.add(host_id, key)
            return
        if not self.prompter.confirm_host_key(host_id, check.key_type, check.fingerprint):
            raise HostKeyRejected(f"Host key for {host_id} was not accepted.")
        known.add(host_id, key)
