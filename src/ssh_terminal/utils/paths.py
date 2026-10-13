"""Filesystem locations used by the application.

* The SSH configuration lives where OpenSSH expects it (``~/.ssh/config``).
* Application metadata (favorites, groups, theme...) lives in a per-user
  application directory, never inside the SSH config:

  - Linux:   ``$XDG_CONFIG_HOME/veylix`` (default ``~/.config/veylix``)
  - Windows: ``%APPDATA%\\VeyLix``
  - macOS:   ``~/Library/Application Support/VeyLix``

Before the rename to VeyLix the app was called SSHDesk; :func:`migrate_legacy_app_dir`
moves the old directory on first start so nothing is lost.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ssh_terminal.utils.platform import is_macos, is_windows

APP_DIR_NAME_WINDOWS = "VeyLix"
APP_DIR_NAME_UNIX = "veylix"
ENV_APP_DIR = "VEYLIX_HOME"  # override used by tests and portable installs
LEGACY_ENV_APP_DIR = "SSHDESK_HOME"  # same, from before the rename (still honoured)
LEGACY_DIR_NAME_WINDOWS = "SSHDesk"
LEGACY_DIR_NAME_UNIX = "sshdesk"


def home_dir() -> Path:
    """The current user's home directory (never assumes a user name)."""
    return Path.home()


def get_ssh_dir() -> Path:
    """``~/.ssh`` on Linux/macOS, ``%USERPROFILE%\\.ssh`` on Windows."""
    if is_windows():
        profile = os.environ.get("USERPROFILE")
        if profile:
            return Path(profile) / ".ssh"
    return home_dir() / ".ssh"


def get_ssh_config_path() -> Path:
    """Path of the user's OpenSSH client configuration file."""
    return get_ssh_dir() / "config"


def get_known_hosts_path() -> Path:
    return get_ssh_dir() / "known_hosts"


def get_app_dir() -> Path:
    """Directory holding ``app_config.json``, logs and backups."""
    override = os.environ.get(ENV_APP_DIR) or os.environ.get(LEGACY_ENV_APP_DIR)
    if override:
        return Path(override)
    return _app_dir_named(APP_DIR_NAME_WINDOWS, APP_DIR_NAME_UNIX)


def get_legacy_app_dir() -> Path:
    """Where SSHDesk (the app's former name) kept its data."""
    return _app_dir_named(LEGACY_DIR_NAME_WINDOWS, LEGACY_DIR_NAME_UNIX)


def _app_dir_named(windows_name: str, unix_name: str) -> Path:
    if is_windows():
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home_dir() / "AppData" / "Roaming"
        return base / windows_name
    if is_macos():
        return home_dir() / "Library" / "Application Support" / windows_name
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home_dir() / ".config"
    return base / unix_name


def migrate_legacy_app_dir() -> Path | None:
    """First start after the rename: move the SSHDesk data directory to the VeyLix one.

    Returns the old directory when something was moved. Settings, team files, the
    encrypted vault, logs and backups come along; the team ``Include`` in
    ``~/.ssh/config`` is updated by :class:`TeamService` on its next sync.
    """
    if os.environ.get(ENV_APP_DIR) or os.environ.get(LEGACY_ENV_APP_DIR):
        return None  # explicit directory: nothing to migrate
    new, old = get_app_dir(), get_legacy_app_dir()
    if new.exists() or not old.is_dir() or new == old:
        return None
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(old), str(new))
    except OSError:
        shutil.copytree(old, new, dirs_exist_ok=True)  # e.g. a file still open on Windows
    return old


def get_app_config_path() -> Path:
    return get_app_dir() / "app_config.json"


def get_log_dir() -> Path:
    return get_app_dir() / "logs"


def get_backup_dir() -> Path:
    return get_app_dir() / "backups"


def resources_dir() -> Path:
    """Bundled resources (icons, styles). Works from source and from PyInstaller."""
    return Path(__file__).resolve().parent.parent / "resources"


def expand_user_path(value: str) -> Path:
    """Expand ``~`` and environment variables the way OpenSSH users expect.

    ``~`` is expanded with :func:`get_ssh_dir`'s notion of home so that Windows
    paths such as ``~/.ssh/id_ed25519`` resolve to ``%USERPROFILE%\\.ssh``.
    """
    value = value.strip().strip('"')
    if value.startswith("~/") or value.startswith("~\\") or value == "~":
        value = str(get_ssh_dir().parent) + value[1:]
    return Path(os.path.expandvars(os.path.expanduser(value)))


def contract_user_path(path: Path | str) -> str:
    """Inverse of :func:`expand_user_path`: ``/home/x/.ssh/id`` -> ``~/.ssh/id``."""
    p = Path(path)
    home = get_ssh_dir().parent
    try:
        rel = p.resolve().relative_to(home.resolve())
    except (ValueError, OSError):
        return str(p)
    return "~/" + rel.as_posix()
