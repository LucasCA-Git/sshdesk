"""Allow ``python -m ssh_terminal``."""

import sys

from ssh_terminal.main import main

if __name__ == "__main__":
    sys.exit(main())
