"""Small reusable dialogs: friendly errors, confirmations, credential prompts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.errors import FriendlyError
from ssh_terminal.ui.theme import current_palette, icon


def mark_primary(button: QPushButton, danger: bool = False) -> QPushButton:
    button.setProperty("danger" if danger else "primary", True)
    button.style().unpolish(button)
    button.style().polish(button)
    return button


class ErrorDialog(QDialog):
    """Friendly error message with a 'View technical details' expander."""

    def __init__(self, error: FriendlyError, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(error.title)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        header = QHBoxLayout()
        badge = QLabel()
        badge.setPixmap(icon("x", current_palette().danger).pixmap(22, 22))
        header.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        title = QLabel(error.title)
        title.setObjectName("DialogHeader")
        header.addWidget(title, 1)
        layout.addLayout(header)

        message = QLabel(error.message)
        message.setWordWrap(True)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(message)

        if error.causes:
            causes = QLabel("Possible causes:\n\n" + "\n".join(f"•  {c}" for c in error.causes))
            causes.setWordWrap(True)
            causes.setProperty("muted", True)
            layout.addWidget(causes)

        self.details = QPlainTextEdit(error.details)
        self.details.setReadOnly(True)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.details.setMinimumHeight(180)
        self.details.setVisible(False)
        layout.addWidget(self.details)

        buttons = QHBoxLayout()
        if error.details:
            toggle = QPushButton("View technical details")
            toggle.setCheckable(True)
            toggle.toggled.connect(self._toggle)
            buttons.addWidget(toggle)
            copy = QPushButton("Copy details")
            copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(error.as_text() + "\n\n" + error.details))
            buttons.addWidget(copy)
        buttons.addStretch(1)
        ok = mark_primary(QPushButton("OK"))
        ok.clicked.connect(self.accept)
        ok.setDefault(True)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def _toggle(self, on: bool) -> None:
        self.details.setVisible(on)
        self.adjustSize()


def show_error(parent: QWidget | None, error: FriendlyError) -> None:
    ErrorDialog(error, parent).exec()


def confirm(parent: QWidget | None, title: str, text: str, ok_text: str = "OK", danger: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    ok = box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)
    mark_primary(ok, danger=danger)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is ok


class SecretDialog(QDialog):
    """Password / passphrase prompt with optional 'remember in keyring'."""

    def __init__(self, title: str, prompt: str, can_remember: bool, parent: QWidget | None = None, remember_label: str = "Save in system keyring") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        lock = QLabel()
        lock.setPixmap(icon("key", current_palette().accent).pixmap(22, 22))
        header.addWidget(lock)
        label = QLabel(prompt)
        label.setWordWrap(True)
        header.addWidget(label, 1)
        layout.addLayout(header)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.edit)
        self.remember = QCheckBox(remember_label)
        self.remember.setVisible(can_remember)
        layout.addWidget(self.remember)
        if not can_remember:
            note = QLabel("System keyring unavailable — the secret is kept in memory for this session only.")
            note.setWordWrap(True)
            note.setProperty("muted", True)
            layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        mark_primary(buttons.button(QDialogButtonBox.StandardButton.Ok))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.edit.setFocus()


class HostKeyDialog(QDialog):
    def __init__(self, host: str, key_type: str, fingerprint: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Unknown host key")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        title = QLabel(f"The authenticity of host <b>{host}</b> can't be established.")
        title.setWordWrap(True)
        layout.addWidget(title)
        fp = QLabel(f"{key_type} key fingerprint:<br><code>{fingerprint}</code>")
        fp.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fp.setWordWrap(True)
        layout.addWidget(fp)
        hint = QLabel(
            "Verify this fingerprint with the server administrator (or with "
            "<code>ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub</code> on the server). "
            "If you continue, the key is added to your known_hosts file."
        )
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        accept = mark_primary(QPushButton("Accept and Connect"))
        accept.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        cancel.setDefault(True)


class KeyboardInteractiveDialog(QDialog):
    def __init__(self, title: str, instructions: str, prompts: list[tuple[str, bool]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or "Authentication")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        if instructions:
            text = QLabel(instructions)
            text.setWordWrap(True)
            layout.addWidget(text)
        form = QFormLayout()
        self.edits: list[QLineEdit] = []
        for prompt, echo in prompts:
            edit = QLineEdit()
            if not echo:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(prompt.strip(), edit)
            self.edits.append(edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        mark_primary(buttons.button(QDialogButtonBox.StandardButton.Ok))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.edits:
            self.edits[0].setFocus()

    def answers(self) -> list[str]:
        return [e.text() for e in self.edits]


def monospace_font(size: int = 10) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(size)
    return font
