"""Platform detection helpers.

Everything that depends on the operating system goes through this module so the
rest of the code base never calls ``platform.system()`` directly.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


def system_name() -> str:
    """Return ``"Windows"``, ``"Linux"`` or ``"Darwin"`` (``platform.system()``)."""
    return platform.system()


def is_windows() -> bool:
    return system_name() == "Windows"


def is_linux() -> bool:
    return system_name() == "Linux"


def is_macos() -> bool:
    return system_name() == "Darwin"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def os_pretty_name() -> str:
    """Human readable OS name, e.g. ``Windows 11 (build 22631)`` or ``Ubuntu 24.04 LTS``."""
    if is_windows():
        try:
            build = sys.getwindowsversion().build  # type: ignore[attr-defined]
        except AttributeError:
            build = 0
        version = "11" if build >= 22000 else platform.release()
        return f"Windows {version} (build {build})"
    if is_linux():
        try:
            info = platform.freedesktop_os_release()
            return info.get("PRETTY_NAME") or f"Linux {platform.release()}"
        except OSError:
            return f"Linux {platform.release()}"
    return f"{system_name()} {platform.release()}"


@dataclass(frozen=True)
class ShellProfile:
    """A local shell that can be opened in a terminal tab."""

    name: str
    argv: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.name.lower()


def parse_wsl_distros(raw: bytes) -> list[str]:
    """Names from ``wsl.exe -l -q`` (UTF-16LE on most Windows builds), Docker's internal ones skipped."""
    text = raw.decode("utf-16-le", errors="ignore") if b"\x00" in raw else raw.decode("utf-8", errors="ignore")
    names = [line.strip().strip("﻿\x00") for line in text.splitlines()]
    return [n for n in names if n and not n.lower().startswith("docker-desktop")]


def _wsl_distros(wsl: str) -> list[str]:
    import subprocess

    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            [wsl, "-l", "-q"], capture_output=True, timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_wsl_distros(out.stdout) if out.returncode == 0 else []


def _find_git_bash() -> str | None:
    git = shutil.which("git.exe")
    candidates = [Path(git).resolve().parent.parent / "bin" / "bash.exe"] if git else []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [Path(base) / "Git" / "bin" / "bash.exe", Path(base) / "Programs" / "Git" / "bin" / "bash.exe"]
    return next((str(c) for c in candidates if c.is_file()), None)


def describe_exit_status(status: int | None) -> str:
    """Human text for a process exit code, including Windows NTSTATUS values."""
    if status is None:
        return "Process exited"
    code = status & 0xFFFFFFFF
    known = {
        0xC000013A: "the console was closed or interrupted (Ctrl+C)",
        0xC0000005: "it crashed (access violation)",
        0xC0000135: "a required DLL was not found",
        0xC0000142: "it failed to initialize",
        0xC00000FD: "stack overflow",
    }
    if code in known:
        return f"Process exited: {known[code]} (0x{code:08X})"
    if code >= 0xC0000000:
        return f"Process exited with error 0x{code:08X}"
    return f"Process exited (status {status})"


def detect_local_shells() -> list[ShellProfile]:
    """Detect the interactive shells available on this machine.

    The first entry is the preferred default for the platform.
    """
    profiles: list[ShellProfile] = []
    if is_windows():
        pwsh = shutil.which("pwsh.exe")
        if pwsh:
            profiles.append(ShellProfile("PowerShell 7", [pwsh, "-NoLogo"]))
        powershell = shutil.which("powershell.exe")
        if powershell:
            profiles.append(ShellProfile("Windows PowerShell", [powershell, "-NoLogo"]))
        cmd = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
        profiles.append(ShellProfile("Command Prompt", [cmd]))
        git_bash = _find_git_bash()
        if git_bash:
            profiles.append(ShellProfile("Git Bash", [git_bash, "--login", "-i"]))
        wsl = shutil.which("wsl.exe")
        if wsl:
            distros = _wsl_distros(wsl)
            profiles.append(ShellProfile("WSL", [wsl]))  # the default distribution
            profiles.extend(ShellProfile(f"WSL · {name}", [wsl, "-d", name]) for name in distros)
        return profiles

    seen: set[str] = set()
    preferred = os.environ.get("SHELL")
    if not preferred:
        try:
            import pwd

            preferred = pwd.getpwuid(os.getuid()).pw_shell
        except (ImportError, KeyError):
            preferred = None
    candidates = [preferred] if preferred else []
    candidates += ["zsh", "bash", "fish", "sh"] if is_macos() else ["bash", "zsh", "fish", "sh"]
    # Apps started from Finder/Dock get a minimal PATH: also look where Homebrew installs shells.
    search = os.environ.get("PATH", "")
    if is_macos():
        search = os.pathsep.join([search, "/opt/homebrew/bin", "/usr/local/bin"])
    for candidate in candidates:
        path = shutil.which(candidate, path=search) if candidate else None
        if not path:
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        name = Path(path).name
        profiles.append(ShellProfile(name, [path, "-l"] if name in ("bash", "zsh", "sh") else [path]))
    if not profiles:
        profiles.append(ShellProfile("sh", ["/bin/sh"]))
    return profiles


def default_editor_command(path: Path) -> list[str] | None:
    """Fallback command used to open a text file when the desktop has no association."""
    if is_windows():
        return ["notepad.exe", str(path)]
    for editor in (os.environ.get("VISUAL"), os.environ.get("EDITOR")):
        if editor and shutil.which(editor.split()[0]) and editor.split()[0] not in ("vi", "vim", "nano", "nvim"):
            return [*editor.split(), str(path)]
    for gui in ("gnome-text-editor", "gedit", "kate", "mousepad", "xed", "code"):
        exe = shutil.which(gui)
        if exe:
            return [exe, str(path)]
    return None
