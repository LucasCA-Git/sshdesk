"""Launcher used by PyInstaller and for running from a source checkout (python app.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ssh_terminal.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
