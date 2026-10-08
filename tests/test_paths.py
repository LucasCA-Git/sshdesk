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
    monkeypatch.delenv("SSHDESK_HOME", raising=False)
    monkeypatch.setattr(plat.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert paths.get_app_dir() == tmp_path / "Roaming" / "SSHDesk"


def test_app_dir_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSHDESK_HOME", raising=False)
    monkeypatch.setattr(plat.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths.get_app_dir() == tmp_path / "cfg" / "sshdesk"


def test_expand_and_contract(isolated_home: Path) -> None:
    p = paths.expand_user_path("~/.ssh/id_ed25519")
    assert p == isolated_home / ".ssh" / "id_ed25519"
    assert paths.contract_user_path(p) == "~/.ssh/id_ed25519"
    assert paths.expand_user_path('"~/.ssh/a b"') == isolated_home / ".ssh" / "a b"


def test_detect_shells_not_empty() -> None:
    shells = plat.detect_local_shells()
    assert shells and shells[0].argv
