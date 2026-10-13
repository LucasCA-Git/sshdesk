"""Docker and the SQL console end to end, through a real SSH connection.

Needs what ``SSHDESK_LIVE_SSHD`` needs (``scripts/start_test_sshd.sh``) plus, on that
same machine:

* ``VEYLIX_LIVE_DOCKER=1``: a Docker daemon with containers ``web`` (running, port
  8081->80), ``worker`` (running, prints INFO/WARN/ERROR every second), ``paused-one``
  (paused) and ``done-job`` (exited), all from a local image.
* ``VEYLIX_LIVE_MYSQL=user:password@port`` and/or ``VEYLIX_LIVE_POSTGRES=user:password@port``:
  a database ``shop`` with tables ``customers`` (3 rows, one NULL email) and ``orders``
  (20000 rows); PostgreSQL also has ``billing.invoices``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from ssh_terminal.services.docker_service import DockerClient, DockerError, Problem, database_candidates
from ssh_terminal.services.remote_exec import RemoteRunner, RemoteTimeout
from ssh_terminal.services.sql_service import QueryCancelled, SqlError, SqlProfile, SqlTunnel, open_session
from ssh_terminal.ssh.config_parser import SSHConfigSet
from ssh_terminal.ssh.resolver import SSHConfigResolver
from ssh_terminal.ssh.ssh_auth import NullCredentialStore
from ssh_terminal.ssh.ssh_manager import SSHManager
from ssh_terminal.ssh.ssh_session import ConnectOptions

LIVE = os.environ.get("SSHDESK_LIVE_SSHD")
pytestmark = pytest.mark.skipif(not LIVE, reason="set SSHDESK_LIVE_SSHD to run live sshd tests")


class AcceptAll:
    def ask_password(self, prompt, can_remember):  # noqa: ANN001, ANN201
        return None

    def ask_passphrase(self, key_path, can_remember):  # noqa: ANN001, ANN201
        return None

    def confirm_host_key(self, host, key_type, fingerprint):  # noqa: ANN001, ANN201
        return True

    def keyboard_interactive(self, title, instructions, prompts):  # noqa: ANN001, ANN201
        return None


class PasswordPrompter(AcceptAll):
    def __init__(self, password: str) -> None:
        self.password = password

    def ask_password(self, prompt, can_remember):  # noqa: ANN001, ANN201
        from ssh_terminal.ssh.ssh_auth import SecretAnswer

        return SecretAnswer(self.password)


@pytest.fixture
def connection(isolated_home: Path):
    d = Path(LIVE)
    cfg = isolated_home / ".ssh" / "config"
    cfg.write_text(
        f"Host box\n    HostName 127.0.0.1\n    Port 2222\n    User root\n    IdentityFile {d}/client_key\n"
        f"    IdentitiesOnly yes\n    UserKnownHostsFile {isolated_home}/.ssh/known_hosts\n",
        encoding="utf-8",
    )
    manager = SSHManager(lambda: SSHConfigSet.load(cfg), SSHConfigResolver(cfg, prefer_openssh=True),
                         NullCredentialStore(), ConnectOptions(timeout=10, use_agent=False))
    conn = manager.connector(AcceptAll()).connect(manager.resolve("box"))
    yield conn
    conn.close()


# ---------------------------------------------------------------------- #
def test_runner_reuses_the_connection_and_times_out(connection) -> None:
    runner = RemoteRunner(connection)
    assert runner.run("echo hi; echo oops >&2; exit 3").__dict__ == {"status": 3, "out": "hi\n", "err": "oops\n"}
    big = runner.run("head -c 300000 /dev/zero | tr '\\0' x; head -c 300000 /dev/zero | tr '\\0' y >&2")
    assert len(big.out) == 300000 and len(big.err) == 300000  # both pipes drained, no deadlock
    with pytest.raises(RemoteTimeout):
        runner.run("sleep 5", timeout=1)
    chunks: list[str] = []
    stop = threading.Event()
    threading.Timer(1.2, stop.set).start()
    result = runner.stream("while true; do echo tickmark; sleep 0.3; done", chunks.append, stop)
    assert result.status == -1 and "".join(chunks).count("tickmark") >= 3
    # closing the channel must end the remote loop too (no orphan left on the server)
    deadline = time.monotonic() + 3
    while runner.run("pgrep -f '[t]ickmark' || true").out.strip() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert runner.run("pgrep -f '[t]ickmark' || true").out.strip() == ""
    assert runner.run("echo still alive").out == "still alive\n"


@pytest.mark.skipif(not os.environ.get("VEYLIX_LIVE_DOCKER"), reason="set VEYLIX_LIVE_DOCKER=1")
def test_docker_lists_acts_and_streams_logs(connection) -> None:
    docker = DockerClient(RemoteRunner(connection))
    summary = docker.summary()
    assert summary.version and summary.images
    by_name = {c.name: c for c in summary.containers}
    assert by_name["web"].state == "running" and "8081->80/tcp" in by_name["web"].ports
    assert by_name["paused-one"].state == "paused" and by_name["done-job"].state == "exited"

    docker.action("stop", "web")
    assert {c.name: c for c in docker.containers()}["web"].state == "exited"
    docker.action("start", "web")
    assert {c.name: c for c in docker.containers()}["web"].state == "running"
    docker.action("unpause", "paused-one")
    docker.action("pause", "paused-one")
    assert {c.name: c for c in docker.containers()}["paused-one"].state == "paused"

    with pytest.raises(ValueError):
        docker.action("stop", "web; rm -rf /")  # never reaches the shell
    with pytest.raises(DockerError) as info:
        docker.action("start", "no-such-container-xyz")
    assert info.value.problem is Problem.OTHER and "no-such-container-xyz" in str(info.value)

    text: list[str] = []
    stop = threading.Event()
    threading.Timer(2.5, stop.set).start()
    docker.logs("worker", 5, True, True, text.append, stop)
    joined = "".join(text)
    assert "INFO tick" in joined and joined[:4].isdigit()  # follow + timestamps
    tail: list[str] = []
    docker.logs("worker", 3, False, False, tail.append, threading.Event())
    assert len([ln for ln in "".join(tail).splitlines() if ln]) == 3


class _Prefixed:
    """A runner that prefixes every command (to simulate broken environments)."""

    def __init__(self, runner: RemoteRunner, prefix: str) -> None:
        self.runner, self.prefix = runner, prefix

    def run(self, command: str, timeout: float | None = None):  # noqa: ANN201
        return self.runner.run(self.prefix + command, timeout)


@pytest.mark.skipif(not os.environ.get("VEYLIX_LIVE_DOCKER"), reason="set VEYLIX_LIVE_DOCKER=1")
def test_docker_problems_are_classified(connection) -> None:
    runner = RemoteRunner(connection)
    with pytest.raises(DockerError) as info:
        DockerClient(_Prefixed(runner, "PATH=/nonexistent ")).version()
    assert info.value.problem is Problem.NOT_INSTALLED
    with pytest.raises(DockerError) as info:
        DockerClient(_Prefixed(runner, "DOCKER_HOST=unix:///tmp/none.sock ")).version()
    assert info.value.problem is Problem.DAEMON_DOWN


@pytest.mark.skipif(not os.environ.get("VEYLIX_LIVE_DOCKER"), reason="set VEYLIX_LIVE_DOCKER=1")
def test_docker_database_candidates(connection) -> None:
    from ssh_terminal.services.docker_service import Container

    fake = [Container("1", "db", "postgres:16", "Up", "running", "0.0.0.0:5433->5432/tcp, :::5433->5432/tcp"),
            Container("2", "my", "library/mariadb:11", "Up", "running", "127.0.0.1:3307->3306/tcp"),
            Container("3", "web", "nginx", "Up", "running", "0.0.0.0:80->80/tcp")]
    found = database_candidates(fake + DockerClient(RemoteRunner(connection)).containers())
    assert [(c.container, c.kind, c.port) for c in found] == [("db", "postgres", 5433), ("my", "mysql", 3307)]


# ---------------------------------------------------------------------- #
def _profile(env: str, kind: str) -> tuple[SqlProfile, str]:
    creds, port = os.environ[env].rsplit("@", 1)
    user, password = creds.split(":", 1)
    return SqlProfile(kind=kind, port=int(port), user=user, database="shop"), password


@pytest.mark.parametrize("env,kind", [("VEYLIX_LIVE_MYSQL", "mysql"), ("VEYLIX_LIVE_POSTGRES", "postgres")])
def test_sql_console_through_the_tunnel(connection, env: str, kind: str) -> None:
    if not os.environ.get(env):
        pytest.skip(f"set {env}")
    profile, password = _profile(env, kind)
    tunnel = SqlTunnel(connection, profile.host, profile.port)
    assert tunnel.active and tunnel.local_port > 0
    session = open_session(profile, password, tunnel.local_port)
    try:
        assert session.version.startswith(("MySQL", "MariaDB", "PostgreSQL")) and session.database == "shop"
        assert "shop" in session.databases()
        assert {"customers", "orders"} <= set(session.tables())
        cols = {c.name: c for c in session.columns("customers")}
        assert cols["id"].key == "PRI" and not cols["name"].nullable and cols["email"].nullable
        assert any(unique for _n, _c, unique in session.indexes("customers"))
        assert session.count("orders") == 20000

        result = session.execute("SELECT id, name, email FROM customers ORDER BY id")
        assert result.columns == ["id", "name", "email"] and len(result.rows) == 3 and result.rows[1][2] is None
        assert result.elapsed_ms >= 0 and not result.truncated
        big = session.execute("SELECT * FROM orders ORDER BY id", max_rows=100)
        assert len(big.rows) == 100 and big.truncated
        assert session.execute("SELECT 1").rows[0][0] == 1  # connection still usable after truncation
        session.execute("UPDATE customers SET city = NULL WHERE name = 'Carla'")  # repeatable
        changed = session.execute("UPDATE customers SET city = 'Recife' WHERE city IS NULL")
        assert changed.affected == 1 and not changed.has_rows
        with pytest.raises(SqlError) as info:
            session.execute("SELEC oops")
        assert "syntax" in str(info.value).lower()

        # cancel a long query from another thread
        threading.Timer(1.0, session.cancel).start()
        slow = "SELECT SLEEP(10)" if kind == "mysql" else "SELECT pg_sleep(10)"
        started = time.monotonic()
        try:
            session.execute(slow)
        except QueryCancelled:
            pass
        assert time.monotonic() - started < 6
        assert session.execute("SELECT 2").rows[0][0] == 2

        if kind == "postgres":
            assert "billing.invoices" in session.tables()
            session.use("postgres")
            assert session.database == "postgres" and "customers" not in session.tables()
            session.use("shop")
    finally:
        session.close()

    ro = SqlProfile(kind=kind, port=profile.port, user=profile.user, database="shop", read_only=True)
    session = open_session(ro, password, tunnel.local_port)
    try:
        with pytest.raises(SqlError, match="read-only"):
            session.execute("DELETE FROM customers WHERE id = 1")
        assert len(session.execute("SELECT * FROM customers").rows) == 3
    finally:
        session.close()
        tunnel.close()
    assert not tunnel.active


def test_sql_bad_password_is_reported(connection) -> None:
    env = "VEYLIX_LIVE_POSTGRES" if os.environ.get("VEYLIX_LIVE_POSTGRES") else "VEYLIX_LIVE_MYSQL"
    if not os.environ.get(env):
        pytest.skip("no database configured")
    profile, _password = _profile(env, "postgres" if "POSTGRES" in env else "mysql")
    tunnel = SqlTunnel(connection, profile.host, profile.port)
    try:
        with pytest.raises(SqlError) as info:
            open_session(profile, "wrong-password", tunnel.local_port)
        assert "password" in str(info.value).lower() or "denied" in str(info.value).lower()
    finally:
        tunnel.close()


@pytest.mark.skipif(not os.environ.get("VEYLIX_LIVE_DOCKER_USER"), reason="set VEYLIX_LIVE_DOCKER_USER=user:sudo-password")
def test_docker_without_permission_then_sudo(isolated_home: Path) -> None:
    """A user outside the docker group gets PERMISSION; sudo with the password fixes it; a wrong one is reported."""
    user, password = os.environ["VEYLIX_LIVE_DOCKER_USER"].split(":", 1)
    cfg = isolated_home / ".ssh" / "config"
    cfg.write_text(f"Host dev\n    HostName 127.0.0.1\n    Port 2222\n    User {user}\n    PreferredAuthentications password\n"
                   f"    UserKnownHostsFile {isolated_home}/.ssh/known_hosts\n", encoding="utf-8")
    manager = SSHManager(lambda: SSHConfigSet.load(cfg), SSHConfigResolver(cfg, prefer_openssh=True),
                         NullCredentialStore(), ConnectOptions(timeout=10, use_agent=False))
    conn = manager.connector(PasswordPrompter(password)).connect(manager.resolve("dev"))
    try:
        runner = RemoteRunner(conn)
        docker = DockerClient(runner)
        with pytest.raises(DockerError) as info:
            docker.containers()
        assert info.value.problem is Problem.PERMISSION
        runner.enable_sudo("not-the-password")
        with pytest.raises(DockerError) as info:
            docker.containers()
        assert info.value.problem is Problem.SUDO_PASSWORD
        runner.enable_sudo(password)
        assert any(c.name == "web" for c in docker.containers())
        logs: list[str] = []
        docker.logs("worker", 2, False, False, logs.append, threading.Event())
        assert "tick" in "".join(logs) and password not in "".join(logs)  # the password never shows up
    finally:
        conn.close()
