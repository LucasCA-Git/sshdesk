"""Path detection tests (Linux and Windows layouts)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ssh_terminal.utils import paths
from ssh_terminal.utils import platform as plat


def test_ssh_config_path_linux(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plat.platform, "system", lambda: "Linux")
    assert paths.get_ssh_config_path() == isolated_home / ".ssh" / "config"


def test_ssh_config_path_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plat.platform, "system", lambda: "Windows")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Users" / "someone"))
    assert paths.get_ssh_config_path() == tmp_path / "Users" / "someone" / ".ssh" / "config"


def test_app_dir_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYLIX_HOME", raising=False)
    monkeypatch.delenv("SSHDESK_HOME", raising=False)
    monkeypatch.setattr(plat.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert paths.get_app_dir() == tmp_path / "Roaming" / "VeyLix"


def test_app_dir_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYLIX_HOME", raising=False)
    monkeypatch.delenv("SSHDESK_HOME", raising=False)
    monkeypatch.setattr(plat.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths.get_app_dir() == tmp_path / "cfg" / "veylix"


def test_expand_and_contract(isolated_home: Path) -> None:
    p = paths.expand_user_path("~/.ssh/id_ed25519")
    assert p == isolated_home / ".ssh" / "id_ed25519"
    assert paths.contract_user_path(p) == "~/.ssh/id_ed25519"
    assert paths.expand_user_path('"~/.ssh/a b"') == isolated_home / ".ssh" / "a b"


def test_detect_shells_not_empty() -> None:
    shells = plat.detect_local_shells()
    assert shells and shells[0].argv


def test_legacy_sshdesk_dir_is_migrated_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYLIX_HOME", raising=False)
    monkeypatch.delenv("SSHDESK_HOME", raising=False)
    monkeypatch.setattr(plat.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    old = tmp_path / "cfg" / "sshdesk"
    (old / "teams").mkdir(parents=True)
    (old / "app_config.json").write_text('{"groups": {"Prod": ["a"]}}')
    (old / "teams" / "devops.conf").write_text("Host a\n")
    assert paths.migrate_legacy_app_dir() == old
    new = tmp_path / "cfg" / "veylix"
    assert (new / "app_config.json").read_text() == '{"groups": {"Prod": ["a"]}}'
    assert (new / "teams" / "devops.conf").exists() and not old.exists()
    assert paths.migrate_legacy_app_dir() is None  # only once

    # an old SSHDESK_HOME override is still honoured, and never migrated
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path / "portable"))
    assert paths.get_app_dir() == tmp_path / "portable" and paths.migrate_legacy_app_dir() is None


def test_team_include_follows_the_moved_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ssh_terminal.services.team_service import INCLUDE_MARKER, TeamService
    from ssh_terminal.ssh.config_writer import ConfigFileWriter

    monkeypatch.delenv("VEYLIX_HOME", raising=False)
    monkeypatch.delenv("SSHDESK_HOME", raising=False)
    monkeypatch.setattr(plat.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    old_pattern = (tmp_path / "cfg" / "sshdesk" / "teams" / "*.conf").as_posix()
    cfg = tmp_path / "config"
    cfg.write_text("# mine\n# SSHDesk teams (managed by SSHDesk, do not edit the files it includes)\n"
                   f'Include "{old_pattern}"\n\nHost a\n    HostName 10.0.0.1\n')
    teams = TeamService(object(), paths.get_app_dir())
    writer = ConfigFileWriter(tmp_path / "backups")
    assert teams.repoint_legacy_include(cfg, writer)
    text = cfg.read_text()
    assert old_pattern not in text and (tmp_path / "cfg" / "veylix" / "teams" / "*.conf").as_posix() in text
    assert INCLUDE_MARKER in text and text.count("Include") == 1 and "Host a" in text
    assert not teams.repoint_legacy_include(cfg, writer)  # already done
