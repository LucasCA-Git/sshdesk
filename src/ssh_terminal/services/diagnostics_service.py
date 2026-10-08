"""Environment diagnostics (never includes secrets or key contents)."""

from __future__ import annotations

import os
import platform
import sys
from importlib import metadata
from pathlib import Path

from ssh_terminal import __version__
from ssh_terminal.ssh.resolver import find_ssh_binary, openssh_version
from ssh_terminal.utils.paths import get_app_config_path, get_log_dir
from ssh_terminal.utils.platform import is_windows, os_pretty_name


def _version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "not installed"


def agent_status() -> str:
    try:
        import paramiko

        agent = paramiko.Agent()
        try:
            keys = agent.get_keys()
        finally:
            agent.close()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
        return f"Unavailable ({type(exc).__name__})"
    if keys:
        return f"Available ({len(keys)} key{'s' if len(keys) != 1 else ''})"
    if is_windows() or os.environ.get("SSH_AUTH_SOCK"):
        return "Available (no keys loaded)"
    return "Not running (SSH_AUTH_SOCK not set)"


def collect(config_path: Path, host_count: int, keyring_backend: str) -> list[tuple[str, str]]:
    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import qVersion

    ssh_bin = find_ssh_binary()
    return [
        ("SSHDesk", __version__),
        ("Operating System", os_pretty_name()),
        ("Architecture", platform.machine()),
        ("Python", sys.version.split()[0]),
        ("PySide6", f"{pyside_version} (Qt {qVersion()})"),
        ("Paramiko", _version("paramiko")),
        ("pyte", _version("pyte")),
        ("cryptography", _version("cryptography")),
        ("keyring", f"{_version('keyring')} — {keyring_backend}"),
        ("pywinpty", _version("pywinpty") if is_windows() else "n/a (Linux uses native PTY)"),
        ("OpenSSH", (openssh_version(ssh_bin) or "not found") if ssh_bin else "not found"),
        ("ssh binary", ssh_bin or "not found (built-in resolver is used)"),
        ("SSH Config", "Found" if config_path.exists() else "Not found"),
        ("SSH Config Path", str(config_path)),
        ("Hosts", str(host_count)),
        ("SSH Agent", agent_status()),
        ("App config", str(get_app_config_path())),
        ("Logs", str(get_log_dir() / "app.log")),
    ]
