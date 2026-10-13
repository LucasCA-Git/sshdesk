#!/usr/bin/env bash
# Build dist/VeyLix.app and dist/VeyLix-<version>-macos-<arch>.dmg on a Mac.
# PyInstaller does not cross-compile: run it on Apple Silicon for arm64 and on an Intel Mac for x86_64.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv-build
source .venv-build/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest
pyinstaller --noconfirm --clean veylix.spec

VERSION=$(python -c "import re;print(re.search(r'__version__ = \"([^\"]+)\"', open('src/ssh_terminal/__init__.py').read()).group(1))")
ARCH=$(uname -m)                       # arm64 or x86_64
DMG="dist/VeyLix-${VERSION}-macos-${ARCH}.dmg"
STAGE=$(mktemp -d)
cp -R dist/VeyLix.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"    # drag-to-install layout
hdiutil create -volname "VeyLix ${VERSION}" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "Built: $(pwd)/dist/VeyLix.app"
echo "Built: $(pwd)/$DMG"
