"""Port forwarding over an established SSH transport.

* Local  (``ssh -L``): listen locally, forward to ``target`` through the server.
* Remote (``ssh -R``): the server listens, connections come back to ``target``.
* Dynamic(``ssh -D``): a local SOCKS4/4a/5 proxy (CONNECT only, no auth).
"""

from __future__ import annotations

import ipaddress
import itertools
import logging
import select
import socket
import struct
import threading
from dataclasses import dataclass, field

import paramiko

from ssh_terminal.models.connection import PortForwardSpec

log = logging.getLogger(__name__)

_ids = itertools.count(1)


def _shutdown_write(end: object) -> None:
    try:
        if isinstance(end, paramiko.Channel):
            end.shutdown_write()
        else:
            end.shutdown(socket.SHUT_WR)  # type: ignore[attr-defined]
    except (OSError, EOFError, paramiko.SSHException):
        pass


def _pump(src: object, dst: object, counter: list[int], lock: threading.Lock) -> None:
    """Copy src -> dst until EOF; propagate half-close; close both when both directions end."""
    try:
        while True:
            data = src.recv(32768)  # type: ignore[attr-defined]
            if not data:
                break
            dst.sendall(data)  # type: ignore[attr-defined]
        _shutdown_write(dst)
    except (OSError, EOFError, paramiko.SSHException):
        pass
    finally:
        with lock:
            counter[0] -= 1
            last = counter[0] == 0
        if last:
            for end in (src, dst):
                try:
                    end.close()  # type: ignore[attr-defined]
                except (OSError, paramiko.SSHException):
                    pass


def bridge(a: object, b: object, name: str) -> None:
    counter, lock = [2], threading.Lock()
    threading.Thread(target=_pump, args=(a, b, counter, lock), name=f"{name}-up", daemon=True).start()
    threading.Thread(target=_pump, args=(b, a, counter, lock), name=f"{name}-down", daemon=True).start()


@dataclass
class Forwarder:
    spec: PortForwardSpec
    id: int = field(default_factory=lambda: next(_ids))
    connections: int = 0
    error: str | None = None
    active: bool = False

    def stop(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class _ListeningForwarder(Forwarder):
    """Base for forwarders that accept local TCP connections."""

    def __init__(self, spec: PortForwardSpec, transport: paramiko.Transport) -> None:
        super().__init__(spec)
        self.transport = transport
        self._stop = threading.Event()
        family = socket.AF_INET6 if ":" in spec.bind_address else socket.AF_INET
        self._server = socket.socket(family, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: never share a port in use
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((spec.bind_address, spec.bind_port))
        if spec.bind_port == 0:
            self.spec.bind_port = self._server.getsockname()[1]
        self._server.listen(16)
        self.active = True
        self._thread = threading.Thread(target=self._accept_loop, name=f"fwd-{self.id}", daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._server], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                client, peer = self._server.accept()
            except OSError:
                break
            self.connections += 1
            threading.Thread(target=self._handle_safe, args=(client, peer), daemon=True).start()
        self.active = False

    def _handle_safe(self, client: socket.socket, peer: tuple) -> None:
        try:
            self.handle(client, peer)
        except (OSError, paramiko.SSHException, ValueError) as exc:
            log.info("Forward %s: connection from %s failed: %s", self.spec.describe(), peer, exc)
            client.close()

    def handle(self, client: socket.socket, peer: tuple) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def open_channel(self, host: str, port: int, peer: tuple) -> paramiko.Channel:
        return self.transport.open_channel("direct-tcpip", (host, port), (peer[0], peer[1]), timeout=15)

    def stop(self) -> None:
        self._stop.set()
        self.active = False
        try:
            self._server.close()
        except OSError:
            pass


class LocalForwarder(_ListeningForwarder):
    def handle(self, client: socket.socket, peer: tuple) -> None:
        chan = self.open_channel(self.spec.target_host, self.spec.target_port, peer)
        bridge(client, chan, f"L{self.id}")


class DynamicForwarder(_ListeningForwarder):
    """Minimal SOCKS4/4a/5 server (CONNECT command, no authentication)."""

    def handle(self, client: socket.socket, peer: tuple) -> None:
        client.settimeout(15)
        version = self._recv_exact(client, 1)[0]
        if version == 5:
            host, port = self._socks5_handshake(client)
            try:
                chan = self.open_channel(host, port, peer)
            except (paramiko.SSHException, OSError):
                client.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                raise
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        elif version == 4:
            host, port = self._socks4_handshake(client)
            try:
                chan = self.open_channel(host, port, peer)
            except (paramiko.SSHException, OSError):
                client.sendall(b"\x00\x5b\x00\x00\x00\x00\x00\x00")
                raise
            client.sendall(b"\x00\x5a\x00\x00\x00\x00\x00\x00")
        else:
            raise ValueError(f"unsupported SOCKS version {version}")
        client.settimeout(None)
        bridge(client, chan, f"D{self.id}")

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ValueError("SOCKS client closed the connection")
            buf += chunk
        return buf

    def _socks5_handshake(self, client: socket.socket) -> tuple[str, int]:
        nmethods = self._recv_exact(client, 1)[0]
        methods = self._recv_exact(client, nmethods)
        if 0 not in methods:
            client.sendall(b"\x05\xff")
            raise ValueError("SOCKS5 client requires authentication")
        client.sendall(b"\x05\x00")
        ver, cmd, _rsv, atyp = self._recv_exact(client, 4)
        if ver != 5 or cmd != 1:
            client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            raise ValueError("only SOCKS5 CONNECT is supported")
        if atyp == 1:
            host = str(ipaddress.IPv4Address(self._recv_exact(client, 4)))
        elif atyp == 3:
            length = self._recv_exact(client, 1)[0]
            host = self._recv_exact(client, length).decode("idna")
        elif atyp == 4:
            host = str(ipaddress.IPv6Address(self._recv_exact(client, 16)))
        else:
            raise ValueError("bad SOCKS5 address type")
        port = struct.unpack("!H", self._recv_exact(client, 2))[0]
        return host, port

    def _socks4_handshake(self, client: socket.socket) -> tuple[str, int]:
        cmd = self._recv_exact(client, 1)[0]
        port = struct.unpack("!H", self._recv_exact(client, 2))[0]
        ip = self._recv_exact(client, 4)
        while self._recv_exact(client, 1) != b"\x00":  # user id
            pass
        if cmd != 1:
            raise ValueError("only SOCKS4 CONNECT is supported")
        if ip[:3] == b"\x00\x00\x00" and ip[3] != 0:  # SOCKS4a: hostname follows
            name = b""
            while (ch := self._recv_exact(client, 1)) != b"\x00":
                name += ch
            return name.decode("idna"), port
        return str(ipaddress.IPv4Address(ip)), port


class RemoteForwarder(Forwarder):
    def __init__(self, spec: PortForwardSpec, manager: ForwardingManager) -> None:
        super().__init__(spec)
        self.manager = manager
        # paramiko keeps a single TCP handler per transport -> the manager
        # dispatches incoming channels by bound port.
        bound = manager.transport.request_port_forward(spec.bind_address, spec.bind_port, handler=manager.remote_dispatch)
        if spec.bind_port == 0:
            self.spec.bind_port = bound
        self.active = True

    def handle_channel(self, chan: paramiko.Channel) -> None:
        self.connections += 1
        try:
            sock = socket.create_connection((self.spec.target_host, self.spec.target_port), timeout=15)
            sock.settimeout(None)
        except OSError as exc:
            log.info("Remote forward %s: cannot reach target: %s", self.spec.describe(), exc)
            chan.close()
            return
        bridge(chan, sock, f"R{self.id}")

    def stop(self) -> None:
        self.active = False
        try:
            self.manager.transport.cancel_port_forward(self.spec.bind_address, self.spec.bind_port)
        except (paramiko.SSHException, EOFError, OSError) as exc:
            log.info("cancel_port_forward failed: %s", exc)


class ForwardingManager:
    """Owns all forwards of one SSH connection."""

    def __init__(self, transport: paramiko.Transport) -> None:
        self.transport = transport
        self._forwards: dict[int, Forwarder] = {}
        self._remote_by_port: dict[int, RemoteForwarder] = {}
        self._lock = threading.Lock()

    def remote_dispatch(self, chan: paramiko.Channel, origin: tuple, server: tuple) -> None:
        forwarder = self._remote_by_port.get(server[1])
        if forwarder is None:
            chan.close()
            return
        threading.Thread(target=forwarder.handle_channel, args=(chan,), daemon=True).start()

    def start(self, spec: PortForwardSpec) -> Forwarder:
        forwarder: Forwarder
        if spec.kind == "local":
            forwarder = LocalForwarder(spec, self.transport)
        elif spec.kind == "dynamic":
            forwarder = DynamicForwarder(spec, self.transport)
        elif spec.kind == "remote":
            forwarder = RemoteForwarder(spec, self)
            self._remote_by_port[forwarder.spec.bind_port] = forwarder
        else:
            raise ValueError(f"unknown forward type {spec.kind}")
        with self._lock:
            self._forwards[forwarder.id] = forwarder
        log.info("Port forward started: %s", spec.describe())
        return forwarder

    def stop(self, forward_id: int) -> None:
        with self._lock:
            forwarder = self._forwards.pop(forward_id, None)
        if forwarder is None:
            return
        forwarder.stop()
        if isinstance(forwarder, RemoteForwarder):
            self._remote_by_port.pop(forwarder.spec.bind_port, None)
        log.info("Port forward stopped: %s", forwarder.spec.describe())

    def list(self) -> list[Forwarder]:
        with self._lock:
            return list(self._forwards.values())

    def stop_all(self) -> None:
        for fid in [f.id for f in self.list()]:
            self.stop(fid)
