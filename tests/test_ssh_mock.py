"""SSH layer tests with mocked Paramiko objects (no server required)."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest import mock

import paramiko
import pytest

from ssh_terminal.errors import AuthenticationFailed, HostKeyMismatch, HostKeyRejected, describe_exception
from ssh_terminal.ssh.host_keys import KnownHostsStore, fingerprint_sha256, host_id_for
from ssh_terminal.ssh.resolver import ResolvedConfig
from ssh_terminal.ssh.ssh_auth import Authenticator, AuthOptions, NonInteractivePrompter, NullCredentialStore, SecretAnswer
from ssh_terminal.ssh.ssh_session import ConnectOptions, SSHConnector

SERVER_KEY = paramiko.Ed25519Key.from_private_key_file  # noqa: F841 - reference for type checkers


@pytest.fixture
def server_key(tmp_path: Path) -> paramiko.PKey:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    raw = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    )
    path = tmp_path / "server_key"
    path.write_bytes(raw)
    return paramiko.Ed25519Key.from_private_key_file(str(path))


def cfg(**kw) -> ResolvedConfig:
    base = dict(alias="srv", hostname="10.0.0.10", user="lucas", port=22, identity_files=[], identity_files_explicit=False,
                user_known_hosts_files=[], strict_host_key_checking="ask")
    base.update(kw)
    return ResolvedConfig(**base)


class FakeTransport:
    """Minimal stand-in for paramiko.Transport."""

    def __init__(self, server_key: paramiko.PKey, accept_password: str | None = None, allowed=("publickey", "password")) -> None:
        self.server_key = server_key
        self.accept_password = accept_password
        self.allowed = list(allowed)
        self.authenticated = False
        self.calls: list[str] = []
        self.channels: list[tuple] = []
        self.opts = mock.Mock(key_types=("ssh-ed25519", "rsa-sha2-512"))

    def use_compression(self, flag): self.calls.append(f"compression:{flag}")
    def get_security_options(self): return self.opts
    def start_client(self, timeout=None): self.calls.append("start_client")
    def get_remote_server_key(self): return self.server_key
    def set_keepalive(self, n): self.calls.append(f"keepalive:{n}")
    def is_authenticated(self): return self.authenticated
    def is_active(self): return True
    def close(self): self.calls.append("close")

    def auth_none(self, user):
        raise paramiko.BadAuthenticationType("none not allowed", self.allowed)

    def auth_publickey(self, user, key):
        self.calls.append("publickey")
        raise paramiko.AuthenticationException("rejected")

    def auth_password(self, user, password):
        self.calls.append(f"password:{'ok' if password == self.accept_password else 'bad'}")
        if password == self.accept_password:
            self.authenticated = True
            return []
        raise paramiko.AuthenticationException("bad password")

    def auth_interactive(self, user, handler):
        raise paramiko.BadAuthenticationType("no kbd", self.allowed)

    def open_channel(self, kind, dest, src, timeout=None):
        self.channels.append((kind, dest))
        return mock.Mock(name=f"chan-{dest}")


class Prompter(NonInteractivePrompter):
    def __init__(self, passwords=(), accept=True):
        super().__init__(accept)
        self.passwords = list(passwords)
        self.hostkey_prompts = 0

    def ask_password(self, prompt, can_remember):
        return SecretAnswer(self.passwords.pop(0)) if self.passwords else None

    def confirm_host_key(self, host, key_type, fingerprint):
        self.hostkey_prompts += 1
        return self.accept_new_host_keys


def connector(transport_factory, prompter, resolve=None) -> SSHConnector:
    return SSHConnector(resolve or (lambda a: cfg(alias=a, hostname=a)), prompter, NullCredentialStore(),
                        ConnectOptions(use_agent=False), transport_factory=transport_factory)


def test_password_auth_and_known_hosts(server_key, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: mock.MagicMock())
    kh = tmp_path / "known_hosts"
    transport = FakeTransport(server_key, accept_password="pw")
    prompter = Prompter(passwords=["nope", "pw"])
    conn = connector(lambda sock: transport, prompter).connect(cfg(user_known_hosts_files=[str(kh)]))
    assert transport.authenticated
    assert "password:bad" in transport.calls and "password:ok" in transport.calls
    assert prompter.hostkey_prompts == 1
    assert kh.read_text().startswith("10.0.0.10 ssh-ed25519 ")
    assert conn.is_active


def test_unknown_host_rejected(server_key, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: mock.MagicMock())
    transport = FakeTransport(server_key, accept_password="pw")
    with pytest.raises(HostKeyRejected):
        connector(lambda s: transport, Prompter(["pw"], accept=False)).connect(cfg(user_known_hosts_files=[str(tmp_path / "kh")]))
    assert "close" in transport.calls


def test_strict_yes_rejects_without_prompt(server_key, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: mock.MagicMock())
    prompter = Prompter(["pw"], accept=True)
    with pytest.raises(HostKeyRejected):
        connector(lambda s: FakeTransport(server_key, "pw"), prompter).connect(
            cfg(strict_host_key_checking="yes", user_known_hosts_files=[str(tmp_path / "kh")]))
    assert prompter.hostkey_prompts == 0


def test_accept_new_adds_silently(server_key, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: mock.MagicMock())
    kh = tmp_path / "kh"
    prompter = Prompter(["pw"], accept=False)
    connector(lambda s: FakeTransport(server_key, "pw"), prompter).connect(
        cfg(port=2222, strict_host_key_checking="accept-new", user_known_hosts_files=[str(kh)]))
    assert prompter.hostkey_prompts == 0
    assert kh.read_text().startswith("[10.0.0.10]:2222 ")


def test_host_key_mismatch(server_key, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: mock.MagicMock())
    kh = tmp_path / "kh"
    kh.write_text("10.0.0.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n")
    with pytest.raises(HostKeyMismatch) as info:
        connector(lambda s: FakeTransport(server_key, "pw"), Prompter(["pw"])).connect(cfg(user_known_hosts_files=[str(kh)]))
    friendly = describe_exception(info.value)
    assert "CHANGED" in friendly.message and "ssh-keygen -R" in friendly.details


def test_auth_failure_lists_methods(server_key, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: mock.MagicMock())
    transport = FakeTransport(server_key, accept_password=None, allowed=("publickey",))
    with pytest.raises(AuthenticationFailed) as info:
        connector(lambda s: transport, Prompter()).connect(cfg(user_known_hosts_files=[str(tmp_path / "kh")], strict_host_key_checking="no"))
    assert info.value.allowed == ["publickey"]
    friendly = describe_exception(info.value)
    assert friendly.title == "Authentication failed"
    assert "SSH key rejected" in " ".join(friendly.causes)


def test_proxyjump_chain_uses_direct_tcpip(server_key, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: mock.MagicMock())
    created: list[FakeTransport] = []

    def factory(sock):
        t = FakeTransport(server_key, accept_password="pw")
        t.sock = sock
        created.append(t)
        return t

    kh = str(tmp_path / "kh")

    def resolve(alias):
        return cfg(alias=alias, hostname=f"{alias}.example", strict_host_key_checking="no", user_known_hosts_files=[kh])

    target = cfg(alias="db", hostname="10.0.0.20", port=5432, proxy_jump="jump1,admin@jump2:2200",
                 strict_host_key_checking="no", user_known_hosts_files=[kh])
    conn = connector(factory, Prompter(["pw", "pw", "pw"]), resolve).connect(target)
    assert len(created) == 3 and len(conn.jumps) == 2
    assert created[0].channels == [("direct-tcpip", ("jump2.example", 2200))]
    assert created[1].channels == [("direct-tcpip", ("10.0.0.20", 5432))]
    assert conn.jumps[1].config.user == "admin"
    conn.close()
    assert all("close" in t.calls for t in created)


def test_proxyjump_loop_is_detected(server_key, monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: mock.MagicMock())

    def resolve(alias):
        return cfg(alias=alias, hostname=alias, proxy_jump="a")  # a jumps through a

    with pytest.raises(Exception, match="ProxyJump"):
        connector(lambda s: FakeTransport(server_key, "pw"), Prompter(["pw"] * 20), resolve).connect(resolve("a"))


def test_authenticator_prefers_configured_order(server_key) -> None:
    transport = FakeTransport(server_key, accept_password="pw")
    auth = Authenticator(Prompter(["pw"]), NullCredentialStore(), AuthOptions(use_agent=False))
    result = auth.authenticate(transport, cfg(preferred_authentications=["password", "publickey"]))
    assert result.method == "password" and result.tried == ["password"]


def test_known_hosts_store(server_key, tmp_path) -> None:
    kh = tmp_path / "known_hosts"
    kh.write_text("# comment\n@cert-authority *.example ssh-ed25519 AAAA\ngarbage line\n")
    store = KnownHostsStore([kh])
    hid = host_id_for("h", 2222)
    assert hid == "[h]:2222"
    assert store.check(hid, server_key).status.value == "unknown"
    store.add(hid, server_key)
    assert KnownHostsStore([kh]).check(hid, server_key).status.value == "known"
    assert kh.read_text().startswith("# comment\n@cert-authority")  # never rewritten
    assert fingerprint_sha256(server_key).startswith("SHA256:")


def test_preferred_algorithms_puts_known_first(server_key, tmp_path) -> None:
    kh = tmp_path / "kh"
    store = KnownHostsStore([kh])
    store.add("h", server_key)
    assert store.preferred_algorithms("h", ("rsa-sha2-512", "ssh-ed25519"))[0] == "ssh-ed25519"


def test_describe_network_errors() -> None:
    assert describe_exception(socket.gaierror(-2, "Name or service not known"), "x").title == "Host not found"
    assert "timed out" in describe_exception(TimeoutError(), "x").message
    assert describe_exception(ConnectionRefusedError(), "x").title == "Connection refused"


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="uses cat")
def test_proxy_command_socket_roundtrip() -> None:
    from ssh_terminal.ssh.proxy_command import ProxyCommandSocket

    sock = ProxyCommandSocket("cat")
    sock.settimeout(5)
    sock.sendall(b"hello")
    data = b""
    while len(data) < 5:
        data += sock.recv(5 - len(data))
    assert data == b"hello"
    sock.close()
    assert sock.closed
