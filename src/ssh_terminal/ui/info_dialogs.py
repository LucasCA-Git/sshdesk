"""Diagnostics, welcome, external-change (diff) and about dialogs."""

from __future__ import annotations

import difflib
from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal import __app_name__, __repo_url__, __version__
from ssh_terminal.ui.dialogs import mark_primary, monospace_font
from ssh_terminal.ui.theme import app_icon, current_palette


class DiagnosticsDialog(QDialog):
    def __init__(self, rows: list[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Diagnostics")
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        table = QTableWidget(len(rows), 2)
        table.setHorizontalHeaderLabels(["Item", "Value"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, (k, v) in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(k))
            table.setItem(r, 1, QTableWidgetItem(v))
        layout.addWidget(table)
        buttons = QHBoxLayout()
        copy = QPushButton("Copy to clipboard")
        text = "\n".join(f"{k:<18} {v}" for k, v in rows)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(text))
        buttons.addWidget(copy)
        buttons.addStretch(1)
        close = mark_primary(QPushButton("Close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)


class WelcomeDialog(QDialog):
    """First-run dialog."""

    IMPORT = 2
    CREATE = 3

    def __init__(self, host_count: int, config_exists: bool, config_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Welcome to {__app_name__}")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(72, 72))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        title = QLabel(f"Welcome to {__app_name__}")
        title.setObjectName("DialogHeader")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        if config_exists:
            text = (f"We found your SSH configuration.\n\n{host_count} connection{'s' if host_count != 1 else ''} detected."
                    f"\n\n{config_path}")
        else:
            text = f"No SSH configuration found.\n\nWould you like to create one?\n\n{config_path}"
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cont = QPushButton("Continue")
        cont.clicked.connect(self.reject)
        if config_exists:
            main = mark_primary(QPushButton("Import Configuration"))
            main.clicked.connect(lambda: self.done(self.IMPORT))
        else:
            main = mark_primary(QPushButton("Create SSH Config"))
            main.clicked.connect(lambda: self.done(self.CREATE))
        main.setDefault(True)
        buttons.addWidget(cont)
        buttons.addWidget(main)
        buttons.addStretch(1)
        layout.addLayout(buttons)


class WhatsNewDialog(QDialog):
    """Thanks + release notes, shown once after installing or updating."""

    def __init__(self, releases: list, previous: str = "", fresh_install: bool = False,
                 parent: QWidget | None = None) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QTextBrowser

        super().__init__(parent)
        self.setWindowTitle(f"What's New in {__app_name__} {__version__}")
        self.setMinimumSize(560, 520)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(56, 56))
        header.addWidget(logo)
        titles = QVBoxLayout()
        if fresh_install:
            heading = f"Thanks for installing {__app_name__}!"
        elif previous and previous != __version__:
            heading = f"Thanks for updating to {__app_name__} {__version__}!"
        else:
            heading = f"What's new in {__app_name__} {__version__}"
        title = QLabel(heading)
        title.setObjectName("DialogHeader")
        titles.addWidget(title)
        sub = QLabel(f"Updated from {previous} to {__version__}." if previous and previous != __version__
                     else f"Version {__version__}")
        sub.setProperty("muted", True)
        titles.addWidget(sub)
        header.addLayout(titles, 1)
        layout.addLayout(header)

        thanks = QLabel(
            f"{__app_name__} is free and open source, built for the community. Thank you for using it! "
            "If it helps you, please check out the project on GitHub: leave a star, report bugs, "
            "suggest features or contribute."
        )
        thanks.setWordWrap(True)
        layout.addWidget(thanks)

        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMarkdown("\n\n".join(r.markdown() for r in releases) or "No release notes available.")
        layout.addWidget(self.notes, 1)

        buttons = QHBoxLayout()
        releases_btn = QPushButton("Release Notes on GitHub")
        releases_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"{__repo_url__}/releases")))
        github = mark_primary(QPushButton("★  View on GitHub"))
        github.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(__repo_url__)))
        close = QPushButton("Let's go")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        buttons.addWidget(releases_btn)
        buttons.addStretch(1)
        buttons.addWidget(github)
        buttons.addWidget(close)
        layout.addLayout(buttons)


class DiffHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text: str) -> None:  # noqa: N802
        pal = current_palette()
        fmt = QTextCharFormat()
        if text.startswith("+") and not text.startswith("+++"):
            fmt.setForeground(QColor(pal.success))
        elif text.startswith("-") and not text.startswith("---"):
            fmt.setForeground(QColor(pal.danger))
        elif text.startswith("@@"):
            fmt.setForeground(QColor(pal.accent))
        else:
            return
        self.setFormat(0, len(text), fmt)


class ExternalChangeChoice(Enum):
    RELOAD = "reload"
    KEEP = "keep"


class ExternalChangeDialog(QDialog):
    """'SSH config changed externally' with Reload / Keep Current / View Differences."""

    def __init__(self, path: str, old_text: str, new_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SSH config changed")
        self.setMinimumWidth(520)
        self.choice = ExternalChangeChoice.KEEP
        layout = QVBoxLayout(self)
        label = QLabel(f"<b>SSH config changed externally.</b><br><br>{path}")
        label.setWordWrap(True)
        layout.addWidget(label)
        diff = "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile="loaded in SSHDesk",
                tofile="on disk",
            )
        ) or "(no textual differences)"
        self.diff_view = QPlainTextEdit(diff)
        self.diff_view.setReadOnly(True)
        self.diff_view.setFont(monospace_font())
        self.diff_view.setMinimumHeight(260)
        self.diff_view.hide()
        self._highlighter = DiffHighlighter(self.diff_view.document())
        layout.addWidget(self.diff_view, 1)
        buttons = QHBoxLayout()
        view = QPushButton("View Differences")
        view.setCheckable(True)
        view.toggled.connect(self._toggle_diff)
        buttons.addWidget(view)
        buttons.addStretch(1)
        keep = QPushButton("Keep Current")
        keep.clicked.connect(self.reject)
        reload_ = mark_primary(QPushButton("Reload"))
        reload_.setDefault(True)
        reload_.clicked.connect(self._reload)
        buttons.addWidget(keep)
        buttons.addWidget(reload_)
        layout.addLayout(buttons)

    def _toggle_diff(self, on: bool) -> None:
        self.diff_view.setVisible(on)
        self.adjustSize()

    def _reload(self) -> None:
        self.choice = ExternalChangeChoice.RELOAD
        self.accept()


def about_text() -> str:
    return (
        f"<h3>{__app_name__} {__version__}</h3>"
        "<p>SSH connection manager and terminal for Windows and Linux.</p>"
        "<p>Your <code>~/.ssh/config</code> is the source of truth; SSHDesk only stores UI metadata "
        "(favorites, groups, theme) in its own settings file.</p>"
        "<p>Built with Python, PySide6 (Qt), Paramiko and pyte.<br>Licensed under the MIT License.</p>"
    )
