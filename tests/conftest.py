"""Shared fixtures. Tests never touch the real ~/.ssh or app config."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SAMPLE_CONFIG = """\
# Global options
Include config.d/*

Host server-prod prod
    HostName 10.0.0.10
    User lucas
    Port 22
    IdentityFile ~/.ssh/id_ed25519

Host server-hml
    HostName hml.example.com
    User deploy
    Port 2222
    IdentityFile ~/.ssh/id_rsa

# Jump box
Host bastion
    HostName bastion.example.com
    User lucas

Host database
    HostName 10.0.0.20
    User postgres
    ProxyJump bastion

Host server
    HostName 10.0.0.10
    User lucas
    CustomOption something

Host *.example.com
    User lucas

Match host foo exec "true"
    User matchuser

Host *
    ServerAliveInterval 60
    User defaultuser
"""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME/USERPROFILE/APPDATA and the app dir to a temp directory."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("SSHDESK_HOME", str(tmp_path / "appdir"))
    return home


@pytest.fixture
def ssh_config(isolated_home: Path) -> Path:
    path = isolated_home / ".ssh" / "config"
    path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    return path
