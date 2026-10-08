# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for SSHDesk (Windows and Linux).
#   pyinstaller --noconfirm sshdesk.spec
# Produces dist/SSHDesk.exe (Windows) or dist/SSHDesk (Linux), single file, windowed.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
ICONS = ROOT / "src" / "ssh_terminal" / "resources" / "icons"

hiddenimports = collect_submodules("keyring.backends")
if sys.platform == "win32":
    hiddenimports += ["winpty", "win32ctypes.core"]

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT / "src")],
    datas=[(str(ROOT / "src" / "ssh_terminal" / "resources"), "ssh_terminal/resources")],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SSHDesk",
    console=False,
    icon=str(ICONS / ("app.ico" if sys.platform == "win32" else "app-256.png")),
    upx=False,
)
