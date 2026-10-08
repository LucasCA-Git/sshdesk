# Build a single-file Windows executable: dist\SSHDesk.exe
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
py -3.12 -m venv .venv-build
.\.venv-build\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
python -m pytest
pyinstaller --noconfirm --clean sshdesk.spec
Write-Host "Built: $(Resolve-Path dist\SSHDesk.exe)"
