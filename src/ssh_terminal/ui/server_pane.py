"""Base card for server tools that work over SSH (Docker, SQL console).

Gives every tool the same frame as terminals and file panels: a header you can
drag to dock the card, split/close buttons, a "connecting / problem" page with
action buttons, short toasts, and background tasks whose callbacks are dropped
safely if the card was closed meanwhile.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from html import escape
from typing import Any

import shiboken6
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.errors import describe_exception
from ssh_terminal.i18n import tr
from ssh_terminal.models.connection import ConnectionSpec, SessionState
from ssh_terminal.services.remote_exec import RemoteRunner
from ssh_terminal.ui.dialogs import mark_primary
from ssh_terminal.ui.dock import DragHandle
from ssh_terminal.ui.terminal_tabs import PaneBase, state_color
from ssh_terminal.ui.theme import current_palette, icon, status_dot
from ssh_terminal.ui.workers import run_async

log = logging.getLogger(__name__)


class ServerPane(PaneBase):
    """A card bound to one SSH host. Subclasses build the content and react to ``on_connected``."""

    ICON = "server"
    open_terminal_requested = Signal(object)  # ConnectionSpec: "open a terminal on this host"

    def __init__(self, spec: ConnectionSpec, open_ssh: Callable[..., Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self._open_ssh = open_ssh
        self.connection = None
        self.runner: RemoteRunner | None = None
        self._state = SessionState.IDLE
        self._closed = False
        self.active = False
        self.show_highlight = False
        self.description = ""

        self.setObjectName("PaneCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 2)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        self.stack = QStackedWidget()
        self.content = self.build_content()
        self.stack.addWidget(self.content)
        self.stack.addWidget(self._build_message_page())
        # scrolls instead of forcing its minimum width on the split: the card can be made as narrow as you like
        self.scroll = QScrollArea()
        self.scroll.setObjectName("PaneScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("#PaneScroll, #PaneScroll > QWidget > QWidget { background: transparent; }")
        self.scroll.setWidget(self.stack)
        self.stack.currentChanged.connect(self._fit_stack)
        self._fit_stack()
        layout.addWidget(self.scroll, 1)
        self.toast = QLabel()
        self.toast.setObjectName("Toast")
        self.toast.setWordWrap(True)
        self.toast.setTextFormat(Qt.TextFormat.PlainText)  # server/DB messages are never interpreted as markup
        self.toast.hide()
        layout.addWidget(self.toast)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)
        self.restyle()
        # a child timer dies with the card: a card closed before the event loop runs never connects
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self.connect_host)
        self._start_timer.start(0)

    def _fit_stack(self, _index: int = 0) -> None:
        """Only the page on screen counts for the card's minimum size (the scroll area handles the rest)."""
        current = self.stack.currentWidget()
        for i in range(self.stack.count()):
            page = self.stack.widget(i)
            policy = QSizePolicy.Policy.Preferred if page is current else QSizePolicy.Policy.Ignored
            page.setSizePolicy(policy, policy)
        self.stack.adjustSize()

    # -- to override ---------------------------------------------------------- #
    def kind_title(self) -> str:
        return ""

    def build_content(self) -> QWidget:
        return QWidget()

    def on_connected(self) -> None:
        pass

    def on_closing(self) -> None:
        pass

    def extra_header_buttons(self) -> list[QToolButton]:
        return []

    # -- PaneBase interface ------------------------------------------------- #
    @property
    def title(self) -> str:
        return f"{self.kind_title()} · {self.spec.title}"

    @property
    def state(self) -> SessionState:
        return self._state

    def focus_target(self) -> QWidget:
        return self.content if self.stack.currentWidget() is self.content else self.stack.currentWidget()

    def set_active(self, active: bool, highlight: bool) -> None:
        if (active, highlight) != (self.active, self.show_highlight):
            self.active, self.show_highlight = active, highlight
            self.restyle()

    def close_session(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._toast_timer.stop()
        try:
            self.on_closing()
        finally:
            if self.runner is not None:
                self.runner.close()
            conn, self.connection = self.connection, None
            if conn is not None:  # closing the transport can block: never on the GUI thread
                threading.Thread(target=conn.close, name="tool-close", daemon=True).start()

    # -- building blocks ------------------------------------------------------ #
    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("PaneHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(10, 5, 6, 3)
        row.setSpacing(6)
        self.dot = QLabel()
        self.kind_icon = QLabel()
        self.title_label = QLabel()
        self.title_label.setObjectName("PaneTitle")
        self.subtitle = QLabel()
        row.addWidget(self.dot)
        row.addWidget(self.kind_icon)
        row.addWidget(self.title_label)
        row.addWidget(self.subtitle, 1)
        for button in self.extra_header_buttons():
            row.addWidget(button)
        self.buttons: dict[str, QToolButton] = {}
        for key, icon_name, tip in (
            ("split_right", "split-right", tr("Split right")),
            ("split_down", "split-down", tr("Split down")),
            ("close", "x", tr("Close panel")),
        ):
            button = QToolButton()
            button.setToolTip(tip)
            button.setAutoRaise(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("icon_name", icon_name)
            button.clicked.connect(lambda _c=False, k=key: self.action_requested.emit(self, k))
            row.addWidget(button)
            self.buttons[key] = button
        header.setToolTip(tr("Drag this bar onto another terminal to place it beside or below"))
        self._drag = DragHandle(header, lambda: self.drag_requested.emit(self))
        for label in (self.title_label, self.subtitle, self.kind_icon):
            label.setCursor(Qt.CursorShape.OpenHandCursor)
        return header

    def _build_message_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(24, 24, 24, 24)
        col.addStretch(1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.RichText)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.message)
        self.message_buttons = QHBoxLayout()
        self.message_buttons.setSpacing(8)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addLayout(self.message_buttons)
        row.addStretch(1)
        col.addSpacing(10)
        col.addLayout(row)
        col.addStretch(2)
        return page

    # -- states --------------------------------------------------------------- #
    def show_message(self, html: str, actions: list[tuple[str, Callable[[], None]]] | None = None,
                     primary: int = 0) -> None:
        self.message.setText(html)
        while self.message_buttons.count():
            item = self.message_buttons.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for i, (label, slot) in enumerate(actions or []):
            button = QPushButton(label)
            if i == primary:
                mark_primary(button)
            button.clicked.connect(slot)
            self.message_buttons.addWidget(button)
        self.stack.setCurrentIndex(1)

    def show_content(self) -> None:
        self.stack.setCurrentIndex(0)

    def show_toast(self, text: str, level: str = "info", ms: int = 6000) -> None:
        pal = current_palette()
        color = {"ok": pal.success, "error": pal.danger, "warn": pal.warning}.get(level, pal.accent)
        self.toast.setStyleSheet(f"#Toast {{ color: {pal.text}; background: {pal.surface_alt}; "
                                 f"border-left: 3px solid {color}; padding: 6px 10px; margin: 4px 6px 2px 6px; }}")
        self.toast.setText(text)
        self.toast.show()
        self._toast_timer.start(ms)

    def set_state(self, state: SessionState, description: str = "") -> None:
        self._state = state
        if description:
            self.description = description
        self.restyle()
        self.state_changed.emit(self)

    # -- background work ---------------------------------------------------- #
    def _alive(self) -> bool:
        return shiboken6.isValid(self) and not self._closed

    def task(self, fn: Callable[..., Any], done: Callable[[Any], None] | None = None,
             failed: Callable[[BaseException], None] | None = None,
             progress: Callable[[Any], None] | None = None, name: str = "tool") -> None:
        """Run ``fn`` off the GUI thread; callbacks are skipped if the card was closed meanwhile."""
        def guard(cb):  # noqa: ANN001, ANN202
            return (lambda value: cb(value) if self._alive() else None) if cb is not None else None

        run_async(fn, on_done=guard(done), on_error=guard(failed or self.task_failed),
                  on_progress=guard(progress), pass_progress=progress is not None, name=name)

    def task_failed(self, exc: BaseException) -> None:
        log.info("%s task failed: %s", self.kind_title(), exc)
        self.show_toast(str(exc) or type(exc).__name__, "error", 9000)

    # -- connection ----------------------------------------------------------- #
    def connect_host(self) -> None:
        if self._closed:
            return
        self.set_state(SessionState.CONNECTING)
        self.show_message(tr("Connecting to <b>{host}</b>…", host=escape(self.spec.title)))
        old = self.connection
        self.connection = None
        if old is not None:
            threading.Thread(target=old.close, name="tool-close", daemon=True).start()

        def open_connection():  # noqa: ANN202
            connection = self._open_ssh(self.spec, None)
            if self._closed:  # the card was closed while connecting: don't leak the session
                connection.close()
                return None
            return connection

        self.task(open_connection, self._connected, self._connect_failed, name="tool-connect")

    def connection_lost(self) -> bool:
        """True (and a reconnect is started) when the SSH link dropped since the card connected."""
        if self.connection is not None and not self.connection.is_active:
            self.show_toast(tr("The SSH connection was lost. Reconnecting…"), "warn")
            self.connect_host()
            return True
        return False

    def _connected(self, connection) -> None:  # noqa: ANN001
        if connection is None:
            return
        self.connection = connection
        self.runner = RemoteRunner(connection)
        self.set_state(SessionState.CONNECTED, connection.config.endpoint)
        self.show_content()
        self.on_connected()

    def _connect_failed(self, exc: BaseException) -> None:
        self.set_state(SessionState.FAILED)
        friendly = describe_exception(exc)
        self.show_message(f"<b>{escape(friendly.title)}</b><br>{escape(friendly.message)}",
                          [(tr("Retry"), self.connect_host)])

    # -- look ----------------------------------------------------------------- #
    def restyle(self) -> None:
        pal = current_palette()
        border = pal.accent if (self.active and self.show_highlight) else pal.border
        self.setStyleSheet(
            f"#PaneCard {{ background: {pal.surface}; border: 1px solid {border}; border-radius: 9px; }}"
            f"#PaneHeader {{ background: transparent; }}"
            f"#PaneHeader QToolButton:hover {{ background: rgba(127,127,127,0.18); }}"
            f"QTableView {{ background: {pal.surface}; border: none; border-top: 1px solid {pal.border}; "
            f"gridline-color: {pal.border}; selection-background-color: {pal.selection}; selection-color: {pal.text}; }}"
            f"QHeaderView::section {{ background: {pal.surface_alt}; color: {pal.muted}; border: none; "
            f"border-right: 1px solid {pal.border}; padding: 4px 6px; }}"
            f"QLabel#Muted {{ color: {pal.muted}; }}"
            f"QLabel#SectionTitle {{ color: {pal.text}; font-weight: 600; }}"
        )
        self.dot.setPixmap(status_dot(state_color(self._state), 10).pixmap(10, 10))
        self.kind_icon.setPixmap(icon(self.ICON, pal.accent).pixmap(14, 14))
        self.title_label.setText(self.title)
        self.subtitle.setStyleSheet(f"color: {pal.muted};")
        self.subtitle.setText(self.description)
        for button in self.buttons.values():
            button.setIcon(icon(button.property("icon_name"), pal.muted))
            button.setIconSize(QSize(13, 13))
