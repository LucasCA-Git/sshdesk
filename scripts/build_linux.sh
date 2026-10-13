#!/usr/bin/env bash
# Build a single-file Linux executable: dist/VeyLix
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv-build
source .venv-build/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt
python -m pytest
pyinstaller --noconfirm --clean veylix.spec
echo "Built: $(pwd)/dist/VeyLix"
