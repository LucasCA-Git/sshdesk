"""Team sharing end to end: real SSHDesk Server (in a thread) + TeamService.

Skipped when the server dependencies (fastapi, uvicorn...) are not installed.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import uvicorn  # noqa: E402

from ssh_terminal.models.ssh_host import SSHHost  # noqa: E402
from ssh_terminal.services.team_service import TeamApiError, TeamService, host_to_payload, validate_remote_host  # noqa: E402
from ssh_terminal.ssh.config_parser import SSHConfigSet  # noqa: E402
from ssh_terminal.ssh.config_writer import ConfigFileWriter  # noqa: E402


class MemoryCreds:
    available = True

    def __init__(self):
        self.data = {}

    def _get(self, name):
        return self.data.get(name)

    def _set(self, name, value):
        self.data[name] = value

    def _delete(self, name):
        self.data.pop(name, None)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from sshdesk_server.config import Settings
    from sshdesk_server.main import create_app

    tmp = tmp_path_factory.mktemp("server")
    app = create_app(Settings(data_dir=tmp, database_url=f"sqlite:///{tmp}/db.sqlite", secret_key="k" * 48))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


def test_owner_shares_member_receives(server: str, isolated_home: Path, tmp_path: Path) -> None:
    cfg = isolated_home / ".ssh" / "config"
    cfg.write_text("# my header\n\nHost painel\n    HostName 10.0.0.1\n\nHost *\n    ServerAliveInterval 60\n")
    writer = ConfigFileWriter(tmp_path / "backups")

    owner = TeamService(MemoryCreds(), tmp_path / "owner")
    owner.register(server, "owner@example.com", "password-123", "Owner")
    team = owner.create_team("Infra OPA")
    payload = host_to_payload(SSHHost(alias="db-prod", hostname="10.0.0.20", user="postgres", port=2222, proxy_jump="bastion",
                                      identity_files=["~/.ssh/id_ed25519"], server_alive_interval=30))
    payload["host_keys"] = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"]
    owner.save_host(team.id, payload)
    owner.save_host(team.id, host_to_payload(SSHHost(alias="painel", hostname="10.9.9.9")))  # collides with personal alias
    invite = owner.invite(team.id, "dev@example.com")

    member_creds = MemoryCreds()
    member = TeamService(member_creds, tmp_path / "member")
    with pytest.raises(TeamApiError) as info:
        member.login(server, "dev@example.com", "password-123")
    assert info.value.status == 401
    member.register(server, "dev@example.com", "password-123")
    assert [i["team_name"] for i in member.my_invites()] == ["Infra OPA"]
    assert member.accept_invite(invite["code"]).role == "member"

    result = member.sync(cfg, writer)
    assert result.hosts_written == 1 and result.conflicts == ["painel (Infra OPA)"] and result.include_added
    text = cfg.read_text()
    assert text.startswith("# my header\n# SSHDesk teams")  # header kept first, Include before any Host
    assert text.index("Include") < text.index("Host painel")

    hosts = {h.alias: h for h in SSHConfigSet.load(cfg).concrete_hosts()}
    assert hosts["db-prod"].port == 2222 and hosts["db-prod"].proxy_jump == "bastion"
    assert hosts["painel"].hostname == "10.0.0.1"  # personal host untouched
    assert member.team_for_path(hosts["db-prod"].source_file).name == "Infra OPA"
    assert "[10.0.0.20]:2222 ssh-ed25519" in member.known_hosts_file.read_text()

    # second sync: nothing changed -> 304; Include not duplicated
    assert member.sync(cfg, writer).unchanged
    owner.save_host(team.id, host_to_payload(SSHHost(alias="api", hostname="10.0.0.30", comment="API box"), "Staging EU"))
    again = member.sync(cfg, writer)
    assert not again.unchanged and again.hosts_written == 2 and not again.include_added
    assert cfg.read_text().count("Include") == 1
    # the group travels with the host and survives a restart
    assert member.group_of("api") == "Staging EU" and member.group_of("db-prod") == ""
    assert TeamService(member_creds, tmp_path / "member").group_of("api") == "Staging EU"

    # token survives a restart (keyring), sign out removes managed files
    restarted = TeamService(member_creds, tmp_path / "member")
    assert restarted.signed_in and restarted.account.email == "dev@example.com"
    restarted.logout()
    assert not restarted.signed_in and not list(restarted.dir.glob("*.conf"))
    assert "db-prod" not in {h.alias for h in SSHConfigSet.load(cfg).concrete_hosts()}


def test_client_rejects_unsafe_hosts() -> None:
    good = {"alias": "a", "hostname": "h", "port": 22, "options": [["ServerAliveInterval", "30"]]}
    assert validate_remote_host(good) is None
    assert validate_remote_host(dict(good, options=[["ProxyCommand", "sh -c evil"]]))
    assert validate_remote_host(dict(good, hostname="h\nProxyCommand x"))
    assert validate_remote_host(dict(good, alias="*"))
    assert validate_remote_host(dict(good, identity_file='x"\nLocalCommand y'))


def test_unreachable_server(tmp_path: Path) -> None:
    svc = TeamService(MemoryCreds(), tmp_path)
    with pytest.raises(TeamApiError, match="Cannot reach"):
        svc.login("http://127.0.0.1:9", "a@b.com", "password-123")


def test_sync_never_duplicates_hosts(server: str, isolated_home: Path, tmp_path: Path) -> None:
    """Personal hosts win over team hosts (by name or server); between teams the first one wins."""
    cfg = isolated_home / ".ssh" / "config"
    cfg.write_text("Host painel\n    HostName 10.0.0.1\n    User deploy\n")
    writer = ConfigFileWriter(tmp_path / "backups")
    owner = TeamService(MemoryCreds(), tmp_path / "owner")
    owner.register(server, "dup-owner@example.com", "password-123")
    infra = owner.create_team("Infra")
    dev = owner.create_team("Dev")
    owner.save_host(infra.id, host_to_payload(SSHHost(alias="PAINEL", hostname="10.9.9.9")))         # same name, other case
    owner.save_host(infra.id, host_to_payload(SSHHost(alias="painel-team", hostname="10.0.0.1")))    # same server, other name
    owner.save_host(infra.id, host_to_payload(SSHHost(alias="api", hostname="10.0.0.30", user="ops")))
    owner.save_host(dev.id, host_to_payload(SSHHost(alias="api-dev", hostname="10.0.0.30", user="ops")))  # same as Infra's
    owner.save_host(dev.id, host_to_payload(SSHHost(alias="worker", hostname="10.0.0.40")))

    result = owner.sync(cfg, writer)
    written = {h.alias for h in SSHConfigSet.load(cfg).concrete_hosts() if owner.team_for_path(h.source_file)}
    joined = " | ".join(result.conflicts)
    assert "PAINEL (Infra)" in joined
    assert "painel-team (Infra): same server as your “painel”" in joined
    # the shared "api" server comes from whichever team is synced first, never from both
    assert written in ({"api", "worker"}, {"api-dev", "worker"})
    assert result.hosts_written == 2 and len(result.conflicts) == 3
    assert ("api-dev (Dev): same server as “api” from team “Infra”" in joined
            or "api (Infra): same server as “api-dev” from team “Dev”" in joined)


def test_member_removes_team_host_from_own_list(server: str, isolated_home: Path, tmp_path: Path) -> None:
    cfg = isolated_home / ".ssh" / "config"
    cfg.write_text("Host *\n    ServerAliveInterval 60\n")
    writer = ConfigFileWriter(tmp_path / "backups")
    owner = TeamService(MemoryCreds(), tmp_path / "owner")
    owner.register(server, "hide-owner@example.com", "password-123")
    team = owner.create_team("Ops")
    owner.save_host(team.id, host_to_payload(SSHHost(alias="keep", hostname="10.1.0.1")))
    owner.save_host(team.id, host_to_payload(SSHHost(alias="noise", hostname="10.1.0.2")))
    invite = owner.invite(team.id, "hide-member@example.com")
    creds = MemoryCreds()
    member = TeamService(creds, tmp_path / "member")
    member.register(server, "hide-member@example.com", "password-123")
    info = member.accept_invite(invite["code"])
    member.sync(cfg, writer)

    def aliases() -> set[str]:
        return {h.alias for h in SSHConfigSet.load(cfg).concrete_hosts()}

    assert aliases() == {"keep", "noise"}
    member.hide_host(info, "NOISE")  # any case; removed from the managed file right away
    assert aliases() == {"keep"} and member.hidden_count() == 1
    again = member.sync(cfg, writer, force=True)
    assert again.hidden == 1 and aliases() == {"keep"}
    # survives a restart, never reaches the server (the owner still has both)
    assert TeamService(creds, tmp_path / "member").is_hidden("ops", "noise")
    assert {h["alias"] for h in owner.team_hosts(team.id)} == {"keep", "noise"}
    assert member.restore_hidden() == 1
    member.sync(cfg, writer)
    assert aliases() == {"keep", "noise"}
