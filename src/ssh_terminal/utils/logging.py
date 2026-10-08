"""Application logging.

Logs go to ``<app dir>/logs/app.log`` (rotating). A redaction filter is
installed on every handler as a defence in depth: the code never logs
secrets on purpose, and the filter scrubs anything that looks like one.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ssh_terminal.utils.paths import get_log_dir

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"

_SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passphrase|passwd|secret|token)(\s*[=:]\s*)(\S+)"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


class RedactingFilter(logging.Filter):
    """Scrub credentials from log records before they are written."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = message
        for pattern in _SECRET_PATTERNS:
            if pattern.groups >= 3:
                redacted = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", redacted)
            else:
                redacted = pattern.sub("***REDACTED***", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def setup_logging(debug: bool = False, log_dir: Path | None = None) -> Path:
    """Configure root logging; returns the log file path."""
    directory = log_dir or get_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "app.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redactor = RedactingFilter()
    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    if debug or not getattr(sys, "frozen", False):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(LOG_FORMAT))
        console.setLevel(logging.DEBUG if debug else logging.WARNING)
        console.addFilter(redactor)
        root.addHandler(console)

    # Paramiko is very chatty at DEBUG and may log key material details.
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    return log_file
