"""End-to-end tests against real sshd instances.

Skipped unless ``SSHDESK_LIVE_SSHD`` points to a directory prepared like
``scripts/start_test_sshd.sh`` does (two sshd on 127.0.0.1:2222 and :2223,
keys ``client_key`` and ``client_enc`` (passphrase ``secret123``), root
password ``rootpw123``).
"""

from __future__ import annotations

import os
import socket
import struct
import time
from pathlib import Path

import pytest

from ssh_terminal.errors import AuthenticationFailed, HostKeyMismatch, HostKeyRejected
from ssh_terminal.models.connection import PortForwardSpec, SessionState
from ssh_terminal.ssh.config_parser import SSHConfigSet
from ssh_terminal.ssh.forwarding import ForwardingManager
from ssh_terminal.ssh.resolver import SSHConfigResolver
from ssh_terminal.ssh.sftp_session import SFTPSession
from ssh_terminal.ssh.ssh_auth import NullCredentialStore, SecretAnswer
from ssh_terminal.ssh.ssh_manager import SSHManager
from ssh_terminal.ssh.ssh_session import ConnectOptions
from ssh_terminal.terminal.backends.ssh_backend import SSHBackend

LIVE = os.environ.get("SSHDESK_LIVE_SSHD")
pytestmark = pytest.mark.skipif(not LIVE, reason="set SSHDESK_LIVE_SSHD to run live sshd tests")


class ScriptedPrompter:
    def __init__(self, passwords=(), passphrases=(), accept_host=True) -> None:
        self.passwords = list(passwords)
        self.passphrases = list(passphrases)
        self.accept_host = accept_host
        self.asked: list[str] = []

    def ask_password(self, prompt, can_remember):
        self.asked.append("password")
        return SecretAnswer(self.passwords.pop(0)) if self.passwords else None

    def ask_passphrase(self, key_path, can_remember):
        self.asked.append("passphrase")
        return SecretAnswer(self.passphrases.pop(0)) if self.passphrases else None

    def confirm_host_key(self, host, key_type, fingerprint):
        self.asked.append(f"hostkey:{host}")
        return self.accept_host

    def keyboard_interactive(self, title, instructions, prompts):
        return None


@pytest.fixture
def live(isolated_home: Path):
    d = Path(LIVE)
    cfg = isolated_home / ".ssh" / "config"
    cfg.write_text(
        f"""Host bastion
    HostName 127.0.0.1
    Port 2222
    User root
    IdentityFile {d}/client_key
    IdentitiesOnly yes

Host target
    HostName 127.0.0.1
    Port 2223
    User root
    IdentityFile {d}/client_key
    IdentitiesOnly yes
    ProxyJump bastion

Host enc
    HostName 127.0.0.1
    Port 2222
    User root
    IdentityFile {d}/client_enc
    IdentitiesOnly yes

Host viacmd
    HostName 127.0.0.1
    Port 2223
    User root
    IdentityFile {d}/client_key
    ProxyCommand ssh -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i {d}/client_key -p 2222 -W %h:%p root@127.0.0.1

Host pw
    HostName 127.0.0.1
    Port 2222
    User root
    PreferredAuthentications password

Host *
    ServerAliveInterval 30
    UserKnownHostsFile {isolated_home}/.ssh/known_hosts
""",
        encoding="utf-8",
    )
    resolver = SSHConfigResolver(cfg, prefer_openssh=True)
    manager = SSHManager(lambda: SSHConfigSet.load(cfg), resolver, NullCredentialStore(), ConnectOptions(timeout=10, use_agent=False))
    return manager, isolated_home


def test_direct_key_auth_and_known_hosts(live) -> None:
    ssh, home = live
    prompter = ScriptedPrompter()
    conn = ssh.connect(ssh.resolve("bastion"), prompter)
    try:
        status, out, _ = conn.exec_command("echo direct-ok")
        assert status == 0 and out.strip() == "direct-ok"
    finally:
        conn.close()
    assert prompter.asked == ["hostkey:[127.0.0.1]:2222"]
    assert "[127.0.0.1]:2222 ssh-ed25519" in (home / ".ssh" / "known_hosts").read_text()
    # second time: key is known, no prompt
    prompter2 = ScriptedPrompter(accept_host=False)
    ssh.connect(ssh.resolve("bastion"), prompter2).close()
    assert prompter2.asked == []


def test_proxyjump(live) -> None:
    ssh, _ = live
    resolved = ssh.resolve("target")
    assert resolved.proxy_jump == "bastion" and resolved.source == "openssh"
    conn = ssh.connect(resolved, ScriptedPrompter())
    try:
        assert len(conn.jumps) == 1
        _, out, _ = conn.exec_command("echo $SSH_CONNECTION")
        assert out.split()[2:] == ["127.0.0.1", "2223"]
    finally:
        conn.close()
    assert not conn.jumps[0].is_active


def test_encrypted_key_passphrase(live) -> None:
    ssh, _ = live
    prompter = ScriptedPrompter(passphrases=["wrong", "secret123"])
    conn = ssh.connect(ssh.resolve("enc"), prompter)
    conn.close()
    assert prompter.asked.count("passphrase") == 2


def test_password_auth(live) -> None:
    ssh, _ = live
    prompter = ScriptedPrompter(passwords=["bad", "rootpw123"])
    conn = ssh.connect(ssh.resolve("pw"), prompter)
    conn.close()
    assert prompter.asked.count("password") == 2


def test_auth_failure_is_friendly(live) -> None:
    ssh, _ = live
    with pytest.raises((AuthenticationFailed, Exception)) as info:
        ssh.connect(ssh.resolve("pw"), ScriptedPrompter(passwords=[]))
    assert info.type.__name__ in ("ConnectionCancelled", "AuthenticationFailed")


def test_host_key_rejected_and_mismatch(live) -> None:
    ssh, home = live
    with pytest.raises(HostKeyRejected):
        ssh.connect(ssh.resolve("bastion"), ScriptedPrompter(accept_host=False))
    (home / ".ssh" / "known_hosts").write_text(
        "[127.0.0.1]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
    )
    with pytest.raises(HostKeyMismatch):
        ssh.connect(ssh.resolve("bastion"), ScriptedPrompter())


def _banner(port: int, socks: bool = False) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        if socks:
            s.sendall(b"\x05\x01\x00")
            assert s.recv(2) == b"\x05\x00"
            s.sendall(b"\x05\x01\x00\x03" + bytes([9]) + b"127.0.0.1" + struct.pack("!H", 2223))
            assert s.recv(10)[1] == 0
        return s.recv(64)


def test_local_and_dynamic_forward(live) -> None:
    ssh, _ = live
    conn = ssh.connect(ssh.resolve("bastion"), ScriptedPrompter())
    try:
        manager = ForwardingManager(conn.transport)
        local = manager.start(PortForwardSpec("local", "127.0.0.1", 0, "127.0.0.1", 2223))
        assert _banner(local.spec.bind_port).startswith(b"SSH-2.0")
        dynamic = manager.start(PortForwardSpec("dynamic", "127.0.0.1", 0))
        assert _banner(dynamic.spec.bind_port, socks=True).startswith(b"SSH-2.0")
        manager.stop_all()
        assert manager.list() == []
    finally:
        conn.close()


def test_remote_forward(live) -> None:
    ssh, _ = live
    conn = ssh.connect(ssh.resolve("bastion"), ScriptedPrompter())
    try:
        manager = ForwardingManager(conn.transport)
        fwd = manager.start(PortForwardSpec("remote", "127.0.0.1", 0, "127.0.0.1", 2223))
        # connect to the server-side port from the server itself
        _, out, _ = conn.exec_command(f"exec 3<>/dev/tcp/127.0.0.1/{fwd.spec.bind_port}; head -c 7 <&3")
        assert out == "SSH-2.0"
    finally:
        conn.close()


def test_sftp(live) -> None:
    ssh, tmp = live
    conn = ssh.connect(ssh.resolve("bastion"), ScriptedPrompter())
    try:
        sftp = SFTPSession(conn)
        names = [e.name for e in sftp.listdir("/")]
        assert "etc" in names
        local = tmp / "up.txt"
        local.write_text("hello sftp")
        sftp.upload(local, "/tmp/sshdesk_sftp_test.txt")
        sftp.download("/tmp/sshdesk_sftp_test.txt", tmp / "down.txt")
        assert (tmp / "down.txt").read_text() == "hello sftp"
        sftp.remove("/tmp/sshdesk_sftp_test.txt")
        sftp.close()
    finally:
        conn.close()


def test_ssh_backend_shell(live) -> None:
    ssh, _ = live
    data = bytearray()
    states: list[SessionState] = []
    closed = []
    backend = SSHBackend(lambda: ssh.resolve("target"), lambda progress: ssh.connector(ScriptedPrompter(), progress=progress))
    backend.set_callbacks(data.extend, lambda s, m: states.append(s), closed.append)
    backend.start(100, 30)
    deadline = time.time() + 15
    while SessionState.CONNECTED not in states and time.time() < deadline and not closed:
        time.sleep(0.05)
    assert SessionState.CONNECTED in states, closed
    backend.write(b"stty size; echo marker-$((40+2))\r")
    while b"marker-42" not in data and time.time() < deadline:
        time.sleep(0.05)
    assert b"marker-42" in data and b"30 100" in data
    backend.write(b"exit\r")
    while not closed and time.time() < deadline:
        time.sleep(0.05)
    assert closed and closed[0].unexpected is False and closed[0].exit_status == 0


def test_test_connection_report(live) -> None:
    ssh, _ = live
    report = ssh.test_connection(ssh.resolve("bastion"), ScriptedPrompter())
    assert report.success, report.steps
    assert [s.name for s in report.steps] == ["DNS", "TCP", "SSH"]
    bad = ssh.resolve("bastion")
    bad.port = 2299
    report = ssh.test_connection(bad, ScriptedPrompter())
    assert not report.success and report.steps[-1].name == "TCP"


def test_proxycommand(live) -> None:
    ssh, _ = live
    conn = ssh.connect(ssh.resolve("viacmd"), ScriptedPrompter())
    try:
        _, out, _ = conn.exec_command("echo $SSH_CONNECTION")
        assert out.split()[2:] == ["127.0.0.1", "2223"]
    finally:
        conn.close()


def test_remote_fs_transfers(live) -> None:
    """Folder upload, remote -> remote copy through ProxyJump, download, recursive delete."""
    from ssh_terminal.services.file_systems import LocalFS, RemoteFS, Transfer

    ssh, tmp = live
    src_dir = tmp / "project"
    (src_dir / "sub").mkdir(parents=True)
    (src_dir / "a.txt").write_text("A" * 300_000)
    (src_dir / "sub" / "b.txt").write_text("bee")
    local = LocalFS()
    bastion = RemoteFS(ssh.connect(ssh.resolve("bastion"), ScriptedPrompter()))
    target = RemoteFS(ssh.connect(ssh.resolve("target"), ScriptedPrompter()))
    try:
        # both test sshd run on this machine, so each side gets its own folder
        dirs = {id(bastion): "/tmp/sshdesk_fs_a", id(target): "/tmp/sshdesk_fs_b"}
        for fs in (bastion, target):
            if fs.exists(dirs[id(fs)]):
                fs.remove(fs.stat(dirs[id(fs)]))
            fs.mkdir(dirs[id(fs)])
        seen = []
        Transfer(local, [local.stat(str(src_dir))], bastion, "/tmp/sshdesk_fs_a").run(lambda p: seen.append(p.done))
        assert seen[-1] == 300_003
        names = [e.name for e in bastion.listdir("/tmp/sshdesk_fs_a/project")]
        assert names == ["sub", "a.txt"]
        Transfer(bastion, [bastion.stat("/tmp/sshdesk_fs_a/project")], target, "/tmp/sshdesk_fs_b").run()
        down = tmp / "down"
        down.mkdir()
        Transfer(target, [target.stat("/tmp/sshdesk_fs_b/project")], local, str(down)).run()
        assert (down / "project" / "sub" / "b.txt").read_text() == "bee"
        assert (down / "project" / "a.txt").stat().st_size == 300_000
        for fs in (bastion, target):
            fs.remove(fs.stat(dirs[id(fs)]))
            assert not fs.exists(dirs[id(fs)])
    finally:
        bastion.close()
        target.close()
