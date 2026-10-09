"""Deterministic Qt shutdown.

When Python exits it destroys module globals and Qt wrappers in an arbitrary
order, which can delete Qt objects after (or while) QApplication goes away:
"QObject: shared QObject was deleted directly", segfaults or "pure virtual
method called" at exit. Closing and deleting everything while the event loop
still exists, then leaving with ``os._exit``, avoids that on every platform.
"""

from __future__ import annotations

import logging
import os
import sys

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


def release_qt_objects() -> None:
    """Close/delete top-level widgets and app-wide helper objects, then flush deferred deletes."""
    app = QApplication.instance()
    if app is None:
        return
    if isinstance(app, QApplication):
        for widget in app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
    from ssh_terminal.terminal import terminal_session

    terminal_session.release_relay()
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def hard_exit(code: int) -> None:
    """Release Qt objects, flush logs/streams and exit without Python's object teardown."""
    try:
        release_qt_objects()
    finally:
        logging.shutdown()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError, AttributeError):
                pass
        os._exit(code)
