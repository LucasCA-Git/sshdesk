"""Qt implementation of :class:`AuthPrompter`.

SSH connections run in worker threads; when they need an answer from the user
they call this prompter, which posts the question to the GUI thread, shows a
modal dialog and blocks the worker until the dialog is closed. A lock makes
concurrent connections ask one question at a time.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QDialog, QWidget

from ssh_terminal.ssh.ssh_auth import SecretAnswer
from ssh_terminal.ui.dialogs import HostKeyDialog, KeyboardInteractiveDialog, SecretDialog


@dataclass
class _Request:
    func: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None


class QtAuthPrompter(QObject):
    _ask = Signal(object)

    def __init__(self, parent_widget: Callable[[], QWidget | None]) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._lock = threading.Lock()
        self._ask.connect(self._run)

    def _run(self, request: _Request) -> None:
        try:
            request.result = request.func()
        finally:
            request.done.set()

    def _call(self, func: Callable[[], Any]) -> Any:
        if QThread.currentThread() is self.thread():
            return func()
        with self._lock:
            request = _Request(func)
            self._ask.emit(request)
            request.done.wait()
            return request.result

    # ------------------------------------------------------------------ #
    def ask_password(self, prompt: str, can_remember: bool) -> SecretAnswer | None:
        def show() -> SecretAnswer | None:
            dlg = SecretDialog("SSH password", prompt, can_remember, self._parent_widget())
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return None
            return SecretAnswer(dlg.edit.text(), dlg.remember.isChecked())

        return self._call(show)

    def ask_passphrase(self, key_path: str, can_remember: bool) -> SecretAnswer | None:
        def show() -> SecretAnswer | None:
            dlg = SecretDialog("Key passphrase", f"Enter passphrase for key\n{key_path}", can_remember, self._parent_widget())
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return None
            return SecretAnswer(dlg.edit.text(), dlg.remember.isChecked())

        return self._call(show)

    def confirm_host_key(self, host: str, key_type: str, fingerprint: str) -> bool:
        def show() -> bool:
            return HostKeyDialog(host, key_type, fingerprint, self._parent_widget()).exec() == QDialog.DialogCode.Accepted

        return bool(self._call(show))

    def keyboard_interactive(self, title: str, instructions: str, prompts: list[tuple[str, bool]]) -> list[str] | None:
        def show() -> list[str] | None:
            dlg = KeyboardInteractiveDialog(title, instructions, prompts, self._parent_widget())
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return None
            return dlg.answers()

        return self._call(show)
