"""Tabs, split panes and the per-pane connection banner."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.errors import FriendlyError
from ssh_terminal.models.app_settings import AppSettings
from ssh_terminal.models.connection import ConnectionKind, ConnectionSpec, SessionState
from ssh_terminal.terminal.backends.base import CloseInfo
from ssh_terminal.terminal.terminal_session import TerminalSession
from ssh_terminal.terminal.terminal_widget import TerminalWidget
from ssh_terminal.ui.dialogs import show_error
from ssh_terminal.ui.theme import current_palette, icon, status_dot

log = logging.getLogger(__name__)

STATE_COLORS = {
    SessionState.CONNECTED: "success",
    SessionState.CONNECTING: "warning",
    SessionState.IDLE: "warning",
    SessionState.DISCONNECTED: "danger",
    SessionState.FAILED: "danger",
}


def state_color(state: SessionState) -> str:
    return getattr(current_palette(), STATE_COLORS[state])


class Banner(QWidget):
    """Thin bar above a terminal: connecting progress, errors, reconnect."""

    reconnect_clicked = Signal()
    details_clicked = Signal()
    edit_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 8, 6)
        self.label = QLabel()
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self.details = QPushButton("Details")
        self.details.clicked.connect(self.details_clicked)
        self.edit = QPushButton("Edit Connection")
        self.edit.clicked.connect(self.edit_clicked)
        self.reconnect = QPushButton("Reconnect")
        self.reconnect.clicked.connect(self.reconnect_clicked)
        self.dismiss = QToolButton()
        self.dismiss.setIcon(icon("x", current_palette().muted))
        self.dismiss.setToolTip("Hide")
        self.dismiss.clicked.connect(self.hide)
        for w in (self.details, self.edit, self.reconnect, self.dismiss):
            layout.addWidget(w)
        self.hide()

    def show_message(self, text: str, level: str = "info", reconnect: bool = False, details: bool = False, edit: bool = False) -> None:
        self.label.setText(text)
        self.setProperty("level", level)
        self.style().unpolish(self)
        self.style().polish(self)
        self.reconnect.setVisible(reconnect)
        self.details.setVisible(details)
        self.edit.setVisible(edit)
        self.dismiss.setVisible(level != "info")
        self.show()


class TerminalPane(QWidget):
    """One terminal + its session + banner."""

    activated = Signal(object)  # self
    state_changed = Signal(object)  # self
    title_changed = Signal(object)  # self
    close_requested = Signal(object)  # self
    edit_requested = Signal(str)  # alias
    context_menu = Signal(object, QPoint)  # self, global pos
    session_closed = Signal(object, object)  # self, CloseInfo

    def __init__(self, spec: ConnectionSpec, session: TerminalSession, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.session = session
        self.terminal_title = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.banner = Banner()
        layout.addWidget(self.banner)
        self.terminal = TerminalWidget(settings)
        layout.addWidget(self.terminal, 1)

        session.setParent(self)
        session.data_received.connect(self.terminal.feed)
        session.state_changed.connect(self._on_state)
        session.closed.connect(self._on_closed)
        self.terminal.input_ready.connect(session.write)
        self.terminal.size_changed.connect(session.resize)
        self.terminal.title_changed.connect(self._on_title)
        self.terminal.focus_received.connect(lambda: self.activated.emit(self))
        self.terminal.context_menu_requested.connect(lambda pos: self.context_menu.emit(self, pos))
        self.banner.reconnect_clicked.connect(self.reconnect)
        self.banner.details_clicked.connect(self._show_details)
        self.banner.edit_clicked.connect(lambda: self.edit_requested.emit(self.spec.alias))

    @property
    def state(self) -> SessionState:
        return self.session.state

    @property
    def title(self) -> str:
        return self.spec.title

    def start(self) -> None:
        cols, rows = self.terminal.grid_size()
        self.session.start(cols, rows)

    def reconnect(self) -> None:
        self.banner.hide()
        self.terminal.feed(b"\r\n")
        self.session.reconnect()
        self.terminal.setFocus()

    def close_session(self) -> None:
        self.session.close()

    def apply_settings(self, settings: AppSettings) -> None:
        self.terminal.apply_settings(settings)
        self.session.auto_reconnect = settings.auto_reconnect
        self.session.max_attempts = settings.reconnect_attempts

    # ------------------------------------------------------------------ #
    def _on_title(self, title: str) -> None:
        self.terminal_title = title
        self.title_changed.emit(self)

    def _on_state(self, state: SessionState, message: str) -> None:
        if state is SessionState.CONNECTING:
            target = self.spec.title
            self.banner.show_message(f"Connecting to <b>{target}</b>… {message}", "info")
        elif state is SessionState.CONNECTED:
            self.banner.hide()
        self.state_changed.emit(self)

    def _on_closed(self, info: CloseInfo) -> None:
        is_ssh = self.spec.kind is ConnectionKind.SSH
        if info.error is not None:
            self.terminal.feed(f"\r\n\x1b[31m[{info.error.title}]\x1b[0m\r\n".encode())
            text = f"<b>{info.error.title}</b> — {info.error.message.splitlines()[0] if info.error.message else ''}"
            self.banner.show_message(text, "error", reconnect=True, details=True, edit=is_ssh and bool(self.spec.alias))
        elif info.unexpected:
            self.terminal.feed(b"\r\n\x1b[33m[Connection lost]\x1b[0m\r\n")
            retry = " Reconnecting automatically…" if self.session.auto_reconnect else ""
            self.banner.show_message(f"<b>Connection lost</b>.{retry}", "warning", reconnect=True)
        elif not (self.session.backend and self.session.backend.user_closed):
            self.terminal.feed(f"\r\n\x1b[90m[{info.reason}]\x1b[0m\r\n".encode())
            self.banner.show_message(info.reason, "warning", reconnect=True)
        self.state_changed.emit(self)
        self.session_closed.emit(self, info)

    def _show_details(self) -> None:
        info = self.session.last_close
        if info and info.error:
            show_error(self, info.error)
        elif info:
            show_error(self, FriendlyError("Session closed", info.reason))


class TabPage(QWidget):
    """Content of one tab: a tree of QSplitters holding TerminalPanes."""

    def __init__(self, first: TerminalPane, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(first)
        self.active_pane: TerminalPane = first

    def panes(self) -> list[TerminalPane]:
        return self.findChildren(TerminalPane)

    def split(self, pane: TerminalPane, new: TerminalPane, orientation: Qt.Orientation) -> None:
        parent = pane.parentWidget()
        if isinstance(parent, QSplitter) and parent.orientation() == orientation:
            parent.insertWidget(parent.indexOf(pane) + 1, new)
            parent.setSizes([1000] * parent.count())
            return
        splitter = QSplitter(orientation)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        if isinstance(parent, QSplitter):
            index = parent.indexOf(pane)
            sizes = parent.sizes()
            parent.replaceWidget(index, splitter)
            parent.setSizes(sizes)
        else:
            self._layout.replaceWidget(pane, splitter)
        splitter.addWidget(pane)
        splitter.addWidget(new)
        pane.show()
        splitter.setSizes([1000, 1000])

    def remove(self, pane: TerminalPane) -> None:
        parent = pane.parentWidget()
        pane.hide()
        pane.setParent(None)
        pane.deleteLater()
        if isinstance(parent, QSplitter) and parent.count() == 1:
            survivor = parent.widget(0)
            grand = parent.parentWidget()
            if isinstance(grand, QSplitter):
                sizes = grand.sizes()
                grand.replaceWidget(grand.indexOf(parent), survivor)
                grand.setSizes(sizes)
            else:
                self._layout.replaceWidget(parent, survivor)
            survivor.show()
            parent.setParent(None)
            parent.deleteLater()
        remaining = self.panes()
        if remaining and self.active_pane is pane:
            self.active_pane = remaining[0]


class TerminalTabs(QTabWidget):
    """Tab widget with status icons, '+' button, middle-click close and reopen stack."""

    current_pane_changed = Signal(object)  # TerminalPane | None
    pane_state_changed = Signal(object)
    new_tab_requested = Signal()
    new_connection_requested = Signal()
    local_shell_requested = Signal(str)
    pane_context_menu = Signal(object, QPoint)
    edit_connection_requested = Signal(str)
    session_closed = Signal(object, object)
    duplicate_requested = Signal(object)  # TerminalPane

    def __init__(self, settings: AppSettings, shell_names: Callable[[], list[str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._shell_names = shell_names
        self.closed_specs: list[list[ConnectionSpec]] = []
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabBar().setUsesScrollButtons(True)
        self.tabBar().setExpanding(False)
        self.tabBar().installEventFilter(self)
        self.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._tab_menu)
        self.tabCloseRequested.connect(self.close_tab)
        self.currentChanged.connect(self._current_changed)

        self.plus = QToolButton(self)
        self.plus.setObjectName("TabPlus")
        self.plus.setToolTip("New tab (local terminal or SSH connection)")
        self.plus.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._plus_menu = QMenu(self.plus)
        self._plus_menu.aboutToShow.connect(self._fill_plus_menu)
        self.plus.setMenu(self._plus_menu)
        self.setCornerWidget(self.plus, Qt.Corner.TopRightCorner)
        self.refresh_icons()

    def refresh_icons(self) -> None:
        self.plus.setIcon(icon("plus", current_palette().muted))
        for i in range(self.count()):
            self._update_tab(self.widget(i))

    def _fill_plus_menu(self) -> None:
        menu = self._plus_menu
        menu.clear()
        for name in self._shell_names():
            menu.addAction(icon("terminal"), name, lambda n=name: self.local_shell_requested.emit(n))
        menu.addSeparator()
        menu.addAction(icon("plus"), "New SSH Connection...", self.new_connection_requested.emit)

    # ------------------------------------------------------------------ #
    def pages(self) -> list[TabPage]:
        return [w for w in (self.widget(i) for i in range(self.count())) if isinstance(w, TabPage)]

    def all_panes(self) -> list[TerminalPane]:
        return [p for page in self.pages() for p in page.panes()]

    def current_page(self) -> TabPage | None:
        w = self.currentWidget()
        return w if isinstance(w, TabPage) else None

    def current_pane(self) -> TerminalPane | None:
        page = self.current_page()
        return page.active_pane if page else None

    def _wire(self, pane: TerminalPane, page: TabPage) -> None:
        pane.activated.connect(lambda p: self._activate(page, p))
        pane.state_changed.connect(lambda p: self._pane_updated(page, p))
        pane.title_changed.connect(lambda p: self._pane_updated(page, p))
        pane.context_menu.connect(self.pane_context_menu)
        pane.edit_requested.connect(self.edit_connection_requested)
        pane.session_closed.connect(self.session_closed)

    def add_pane_tab(self, pane: TerminalPane) -> None:
        page = TabPage(pane)
        self._wire(pane, page)
        index = self.addTab(page, pane.title)
        self.setCurrentIndex(index)
        self._update_tab(page)
        QTimer.singleShot(0, pane.terminal.setFocus)

    def split_pane(self, pane: TerminalPane, new: TerminalPane, orientation: Qt.Orientation) -> None:
        page = self._page_of(pane)
        if page is None:
            return
        self._wire(new, page)
        page.split(pane, new, orientation)
        page.active_pane = new
        QTimer.singleShot(0, new.terminal.setFocus)
        self._update_tab(page)

    def _page_of(self, pane: TerminalPane) -> TabPage | None:
        for page in self.pages():
            if pane in page.panes():
                return page
        return None

    def _activate(self, page: TabPage, pane: TerminalPane) -> None:
        page.active_pane = pane
        if page is self.current_page():
            self.current_pane_changed.emit(pane)
        self._update_tab(page)

    def _pane_updated(self, page: TabPage, pane: TerminalPane) -> None:
        self._update_tab(page)
        if pane is self.current_pane():
            self.pane_state_changed.emit(pane)

    def _update_tab(self, page: QWidget | None) -> None:
        if not isinstance(page, TabPage):
            return
        index = self.indexOf(page)
        if index < 0:
            return
        pane = page.active_pane
        panes = page.panes()
        title = pane.title
        if len(panes) > 1:
            title += f"  [{len(panes)}]"
        self.setTabText(index, title)
        self.setTabIcon(index, status_dot(state_color(pane.state)))
        tip = pane.terminal_title or pane.title
        if pane.session.backend is not None:
            tip += f"\n{pane.session.backend.description}"
        self.setTabToolTip(index, tip)

    def _current_changed(self, _index: int) -> None:
        self.current_pane_changed.emit(self.current_pane())
        pane = self.current_pane()
        if pane is not None:
            QTimer.singleShot(0, pane.terminal.setFocus)

    # ------------------------------------------------------------------ #
    def close_pane(self, pane: TerminalPane) -> None:
        page = self._page_of(pane)
        if page is None:
            return
        if len(page.panes()) <= 1:
            self.close_tab(self.indexOf(page))
            return
        self.closed_specs.append([pane.spec])
        pane.close_session()
        page.remove(pane)
        self._update_tab(page)
        new_active = page.active_pane
        QTimer.singleShot(0, new_active.terminal.setFocus)
        self.current_pane_changed.emit(self.current_pane())

    def close_tab(self, index: int) -> None:
        page = self.widget(index)
        if not isinstance(page, TabPage):
            return
        specs = [p.spec for p in page.panes()]
        if specs:
            self.closed_specs.append(specs)
            del self.closed_specs[:-20]
        for pane in page.panes():
            pane.close_session()
        self.removeTab(index)
        page.deleteLater()
        self.current_pane_changed.emit(self.current_pane())

    def pop_closed(self) -> list[ConnectionSpec] | None:
        return self.closed_specs.pop() if self.closed_specs else None

    def next_tab(self, step: int = 1) -> None:
        if self.count():
            self.setCurrentIndex((self.currentIndex() + step) % self.count())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802, ANN001
        from PySide6.QtCore import QEvent

        if obj is self.tabBar() and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabBar().tabAt(event.position().toPoint())
            if index >= 0:
                self.close_tab(index)
                return True
        return super().eventFilter(obj, event)

    def _tab_menu(self, pos: QPoint) -> None:
        bar: QTabBar = self.tabBar()
        index = bar.tabAt(pos)
        if index < 0:
            return
        page = self.widget(index)
        if not isinstance(page, TabPage):
            return
        menu = QMenu(self)
        menu.addAction("Reconnect", page.active_pane.reconnect)
        menu.addAction("Duplicate", lambda: self.duplicate_requested.emit(page.active_pane))
        menu.addSeparator()
        menu.addAction("Close", lambda: self.close_tab(self.indexOf(page)))
        menu.addAction("Close Other Tabs", lambda: self._close_others(page))
        menu.exec(bar.mapToGlobal(pos))

    def _close_others(self, keep: TabPage) -> None:
        for page in self.pages():
            if page is not keep:
                self.close_tab(self.indexOf(page))
