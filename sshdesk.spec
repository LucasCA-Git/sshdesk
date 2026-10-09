# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for SSHDesk (Windows and Linux).
#   pyinstaller --noconfirm sshdesk.spec
# Produces dist/SSHDesk.exe (Windows) or dist/SSHDesk (Linux) as a single file, and
# dist/SSHDesk.app on macOS (an app bundle; scripts/build_macos.sh wraps it in a .dmg).
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

if sys.platform == "darwin":
    import re

    VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "ssh_terminal" / "__init__.py").read_text()).group(1)
    # macOS: a real .app bundle (onedir inside the bundle; one-file .apps are deprecated).
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="SSHDesk",
        console=False,
        upx=False,
        argv_emulation=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="SSHDesk", upx=False)
    app = BUNDLE(
        coll,
        name="SSHDesk.app",
        icon=str(ICONS / "app-512.png"),  # converted to .icns by PyInstaller (needs Pillow)
        bundle_identifier="io.github.lucasca-git.sshdesk",
        version=VERSION,
        info_plist={
            "CFBundleName": "SSHDesk",
            "CFBundleDisplayName": "SSHDesk",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSHumanReadableCopyright": "© 2026 Lucas Cardoso Alecrim — MIT License",
        },
    )
else:
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
