"""Run blocking functions off the GUI thread and get results back as signals."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

_active: set[TaskBridge] = set()


class TaskBridge(QObject):
    """Lives in the GUI thread; signals emitted from the worker are queued to it."""

    finished = Signal(object)
    failed = Signal(object)
    progress = Signal(object)


def run_async(
    func: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    on_progress: Callable[[Any], None] | None = None,
    pass_progress: bool = False,
    name: str = "worker",
) -> TaskBridge:
    """Call ``func(*args)`` in a daemon thread.

    With ``pass_progress=True`` the function receives an extra ``progress``
    callable as last argument; values passed to it reach ``on_progress`` on
    the GUI thread.
    """
    bridge = TaskBridge()
    _active.add(bridge)
    if on_done:
        bridge.finished.connect(on_done)
    if on_error:
        bridge.failed.connect(on_error)
    if on_progress:
        bridge.progress.connect(on_progress)
    bridge.finished.connect(lambda _r: _active.discard(bridge))
    bridge.failed.connect(lambda _e: _active.discard(bridge))

    def target() -> None:
        try:
            call_args = (*args, bridge.progress.emit) if pass_progress else args
            result = func(*call_args)
        except Exception as exc:  # noqa: BLE001 - forwarded to on_error
            log.debug("Background task %s failed: %s", name, exc)
            bridge.failed.emit(exc)
            return
        bridge.finished.emit(result)

    threading.Thread(target=target, name=name, daemon=True).start()
    return bridge
