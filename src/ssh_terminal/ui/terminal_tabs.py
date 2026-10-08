"""Tabs, split panes and the per-pane connection banner."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor
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
from ssh_terminal.terminal.color_schemes import SCHEMES, ColorScheme
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


def mix(a: str, b: str, ratio: float) -> str:
    """Blend colour ``a`` towards ``b`` (ratio 1 = a)."""
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() * ratio + cb.red() * (1 - ratio)),
        round(ca.green() * ratio + cb.green() * (1 - ratio)),
        round(ca.blue() * ratio + cb.blue() * (1 - ratio)),
    ).name()


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


class PaneHeader(QWidget):
    """Title strip of a terminal card: status dot, name, theme, split, menu and close buttons."""

    def __init__(self, pane: TerminalPane) -> None:
        super().__init__(pane)
        self.setObjectName("PaneHeader")
        self.pane = pane
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 6, 3)
        layout.setSpacing(6)
        self.dot = QLabel()
        self.icon = QLabel()
        self.title = QLabel()
        self.title.setObjectName("PaneTitle")
        self.subtitle = QLabel()
        layout.addWidget(self.dot)
        layout.addWidget(self.icon)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle, 1)
        self.badge = QLabel("BROADCAST")
        self.badge.setToolTip("Typing here is sent to every terminal in this tab")
        self.badge.hide()
        layout.addWidget(self.badge)
        self.buttons: dict[str, QToolButton] = {}
        for key, icon_name, tip in (
            ("broadcast", "broadcast", "Broadcast input: type in all terminals of this tab"),
            ("theme", "palette", "Terminal colors"),
            ("split_right", "split-right", "Split right: open another terminal side by side"),
            ("split_down", "split-down", "Split down: open another terminal below"),
            ("menu", "more", "More"),
            ("close", "x", "Close terminal"),
        ):
            button = QToolButton(self)
            button.setToolTip(tip)
            button.setAutoRaise(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("icon_name", icon_name)
            layout.addWidget(button)
            self.buttons[key] = button
        self.buttons["theme"].setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.theme_menu = QMenu(self)
        self.theme_menu.aboutToShow.connect(self._fill_theme_menu)
        self.buttons["theme"].setMenu(self.theme_menu)
        self.buttons["split_right"].clicked.connect(lambda: pane.action_requested.emit(pane, "split_right"))
        self.buttons["split_down"].clicked.connect(lambda: pane.action_requested.emit(pane, "split_down"))
        self.buttons["close"].clicked.connect(lambda: pane.action_requested.emit(pane, "close"))
        self.buttons["broadcast"].setCheckable(True)
        self.buttons["broadcast"].clicked.connect(lambda: pane.action_requested.emit(pane, "broadcast"))
        self.buttons["menu"].clicked.connect(
            lambda: pane.context_menu.emit(pane, self.buttons["menu"].mapToGlobal(self.buttons["menu"].rect().bottomLeft()))
        )

    def _fill_theme_menu(self) -> None:
        menu = self.theme_menu
        menu.clear()
        current = self.pane.terminal.scheme_override
        follow = menu.addAction("Use app theme")
        follow.setCheckable(True)
        follow.setChecked(current is None)
        follow.triggered.connect(lambda: self.pane.set_theme(None))
        menu.addSeparator()
        for name, scheme in SCHEMES.items():
            action = menu.addAction(status_dot(scheme.highlight, 14), name)
            action.setCheckable(True)
            action.setChecked(current == name)
            action.triggered.connect(lambda _c=False, n=name: self.pane.set_theme(n))

    def refresh(self, scheme: ColorScheme, state: SessionState) -> None:
        muted = mix(scheme.foreground, scheme.background, 0.55)
        self.title.setStyleSheet(f"color: {scheme.foreground};")
        self.subtitle.setStyleSheet(f"color: {muted};")
        self.dot.setPixmap(status_dot(state_color(state), 10).pixmap(10, 10))
        kind_icon = "terminal" if self.pane.spec.kind is ConnectionKind.LOCAL else "server"
        self.icon.setPixmap(icon(kind_icon, scheme.highlight).pixmap(14, 14))
        warn = current_palette().warning
        for key, button in self.buttons.items():
            color = warn if key == "broadcast" and self.pane.broadcast else muted
            button.setIcon(icon(button.property("icon_name"), color))
            button.setIconSize(QSize(13, 13))
        self.buttons["broadcast"].setChecked(self.pane.broadcast)
        self.buttons["broadcast"].setVisible(self.pane.show_highlight or self.pane.broadcast)
        self.badge.setVisible(self.pane.broadcast)
        self.badge.setStyleSheet(
            f"color: {scheme.background}; background: {warn}; border-radius: 4px;"
            "padding: 1px 6px; font-size: 9px; font-weight: 700; letter-spacing: 0.5px;"
        )
        self.title.setText(self.pane.title)
        backend = self.pane.session.backend
        sub = backend.description if backend is not None else ""
        self.subtitle.setText("" if sub == self.pane.title else sub)


class PaneBase(QWidget):
    """Anything that lives in a tab's split tree: a terminal or a file browser.

    Subclasses provide ``spec``, ``title``, ``state``, ``close_session()``,
    ``focus_target()`` and ``set_active(active, highlight)``.
    """

    activated = Signal(object)  # self
    state_changed = Signal(object)  # self
    title_changed = Signal(object)  # self
    context_menu = Signal(object, QPoint)  # self, global pos
    action_requested = Signal(object, str)  # self, "split_right" | "split_down" | "close" | "broadcast"
    terminal_title = ""


class TerminalPane(PaneBase):
    """One terminal card: header + banner + terminal, bound to a session."""

    close_requested = Signal(object)  # self
    edit_requested = Signal(str)  # alias
    session_closed = Signal(object, object)  # self, CloseInfo

    def __init__(self, spec: ConnectionSpec, session: TerminalSession, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.session = session
        self.terminal_title = ""
        self.active = False
        self.show_highlight = False
        self.broadcast = False
        self.setObjectName("PaneCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 2)
        layout.setSpacing(0)
        self.terminal = TerminalWidget(settings)
        self.header = PaneHeader(self)
        layout.addWidget(self.header)
        self.banner = Banner()
        layout.addWidget(self.banner)
        layout.addWidget(self.terminal, 1)
        self.restyle()

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

    def focus_target(self) -> QWidget:
        return self.terminal

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
        self.restyle()

    def set_theme(self, name: str | None) -> None:
        """Per-terminal color scheme (None = follow the app setting)."""
        self.terminal.set_scheme_override(name)
        self.spec.theme = name or ""
        self.restyle()

    def set_active(self, active: bool, highlight: bool) -> None:
        if (active, highlight) != (self.active, self.show_highlight):
            self.active, self.show_highlight = active, highlight
            self.restyle()

    def set_broadcast(self, on: bool) -> None:
        if on != self.broadcast:
            self.broadcast = on
            self.restyle()

    def restyle(self) -> None:
        scheme = self.terminal.scheme
        pal = current_palette()
        border = scheme.highlight if (self.active and self.show_highlight) else pal.border
        if self.broadcast:
            border = pal.warning if self.active else mix(pal.warning, scheme.background, 0.55)
        self.setStyleSheet(
            f"#PaneCard {{ background: {scheme.background}; border: 1px solid {border}; border-radius: 9px; }}"
            f"#PaneHeader {{ background: transparent; }}"
            f"#PaneHeader QToolButton:hover {{ background: rgba(127, 127, 127, 0.18); }}"
        )
        self.header.refresh(scheme, self.state)

    # ------------------------------------------------------------------ #
    def _on_title(self, title: str) -> None:
        self.terminal_title = title
        if title:
            self.header.subtitle.setText(title)
        self.title_changed.emit(self)

    def _on_state(self, state: SessionState, message: str) -> None:
        if state is SessionState.CONNECTING:
            target = self.spec.title
            self.banner.show_message(f"Connecting to <b>{target}</b>… {message}", "info")
        elif state is SessionState.CONNECTED:
            self.banner.hide()
        self.header.refresh(self.terminal.scheme, state)
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
        self.header.refresh(self.terminal.scheme, self.state)
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

    def __init__(self, first: PaneBase, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TabPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 4, 6, 6)
        self._layout.addWidget(first)
        self.active_item: PaneBase = first  # focused pane (terminal or files)
        self._last_terminal: TerminalPane | None = first if isinstance(first, TerminalPane) else None
        self.custom_title = ""
        self.broadcast = False  # mirror typed input to every pane of this tab

    @property
    def active_pane(self) -> TerminalPane | None:
        """The focused terminal (or the last focused one when a file pane has focus)."""
        if isinstance(self.active_item, TerminalPane):
            return self.active_item
        terminals = self.panes()
        if self._last_terminal in terminals:
            return self._last_terminal
        return terminals[0] if terminals else None

    @active_pane.setter
    def active_pane(self, item: PaneBase) -> None:
        self.active_item = item
        if isinstance(item, TerminalPane):
            self._last_terminal = item

    @staticmethod
    def _splitter(orientation: Qt.Orientation) -> QSplitter:
        splitter = QSplitter(orientation)
        splitter.setObjectName("PaneSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        return splitter

    def _root(self) -> QWidget | None:
        item = self._layout.itemAt(0)
        return item.widget() if item is not None else None

    def arrange_grid(self, panes: list[PaneBase] | None = None) -> None:
        """Lay all panes out as an even grid (2 → side by side, 4 → 2×2, 6 → 3×2...)."""
        panes = panes or self.items()
        if len(panes) < 2:
            return
        old_root = self._root()
        for pane in panes:
            pane.setParent(None)
        if old_root is not None and old_root not in panes:
            self._layout.removeWidget(old_root)
            old_root.setParent(None)
            old_root.deleteLater()
        cols = math.ceil(math.sqrt(len(panes)))
        rows = math.ceil(len(panes) / cols)
        outer = self._splitter(Qt.Orientation.Vertical)
        for r in range(rows):
            chunk = panes[r * cols : (r + 1) * cols]
            if len(chunk) == 1:
                outer.addWidget(chunk[0])
                continue
            row = self._splitter(Qt.Orientation.Horizontal)
            for pane in chunk:
                row.addWidget(pane)
            row.setSizes([1000] * len(chunk))
            outer.addWidget(row)
        outer.setSizes([1000] * rows)
        root: QWidget = outer if rows > 1 else outer.widget(0)
        if rows == 1:
            root.setParent(None)
            outer.deleteLater()
        self._layout.addWidget(root)
        for pane in panes:
            pane.show()

    def items(self) -> list[PaneBase]:
        """Every pane of the tab (terminals and file browsers)."""
        return self.findChildren(PaneBase)

    def panes(self) -> list[TerminalPane]:
        """Terminal panes only."""
        return self.findChildren(TerminalPane)

    def split(self, pane: PaneBase, new: PaneBase, orientation: Qt.Orientation) -> None:
        parent = pane.parentWidget()
        if isinstance(parent, QSplitter) and parent.orientation() == orientation:
            parent.insertWidget(parent.indexOf(pane) + 1, new)
            parent.setSizes([1000] * parent.count())
            return
        splitter = self._splitter(orientation)
        if isinstance(parent, QSplitter):
            # insert the new splitter next to the pane, then move the pane into it
            # (QSplitter.replaceWidget loses the nested splitter under PySide)
            index = parent.indexOf(pane)
            sizes = parent.sizes()
            parent.insertWidget(index, splitter)
            splitter.addWidget(pane)
            parent.setSizes(sizes)
        else:
            self._layout.replaceWidget(pane, splitter)
            splitter.addWidget(pane)
        splitter.addWidget(new)
        pane.show()
        splitter.setSizes([1000, 1000])

    def remove(self, pane: PaneBase) -> None:
        parent = pane.parentWidget()
        pane.hide()
        pane.setParent(None)
        pane.deleteLater()
        if isinstance(parent, QSplitter) and parent.count() == 1:
            survivor = parent.widget(0)
            grand = parent.parentWidget()
            if isinstance(grand, QSplitter):
                sizes = grand.sizes()
                grand.insertWidget(grand.indexOf(parent), survivor)
                grand.setSizes(sizes)
            else:
                self._layout.replaceWidget(parent, survivor)
            survivor.show()
            parent.setParent(None)
            parent.deleteLater()
        if self._last_terminal is pane:
            self._last_terminal = None
        remaining = self.items()
        if remaining and self.active_item is pane:
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
    pane_action = Signal(object, str)  # TerminalPane, action
    broadcast_changed = Signal(bool)  # broadcast state of the current tab
    files_requested = Signal()  # new SFTP tab (this computer | host)

    def __init__(self, settings: AppSettings, shell_names: Callable[[], list[str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._shell_names = shell_names
        self.closed_specs: list[list[ConnectionSpec]] = []
        self.setObjectName("TerminalTabs")
        self.tabBar().setDrawBase(False)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(False)
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
        menu.addAction(icon("folder"), "Files (SFTP)", self.files_requested.emit)
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

    def current_item(self) -> PaneBase | None:
        """Focused pane of the current tab, terminal or file browser."""
        page = self.current_page()
        return page.active_item if page else None

    def _wire(self, pane: PaneBase, page: TabPage) -> None:
        pane.activated.connect(lambda p: self._activate(page, p))
        pane.state_changed.connect(lambda p: self._pane_updated(page, p))
        pane.title_changed.connect(lambda p: self._pane_updated(page, p))
        pane.context_menu.connect(self.pane_context_menu)
        pane.action_requested.connect(self.pane_action)
        if isinstance(pane, TerminalPane):
            pane.edit_requested.connect(self.edit_connection_requested)
            pane.session_closed.connect(self.session_closed)
            pane.terminal.user_input.connect(lambda data, src=pane: self._broadcast_input(page, src, data))
            pane.set_broadcast(page.broadcast)

    def add_pane_tab(self, pane: PaneBase) -> TabPage:
        page = TabPage(pane)
        self._wire(pane, page)
        index = self.addTab(page, pane.title)
        self.setCurrentIndex(index)
        self._update_tab(page)
        QTimer.singleShot(0, pane.focus_target().setFocus)
        return page

    def split_pane(self, pane: PaneBase, new: PaneBase, orientation: Qt.Orientation) -> None:
        page = self._page_of(pane)
        if page is None:
            return
        self._wire(new, page)
        page.split(pane, new, orientation)
        page.active_pane = new
        QTimer.singleShot(0, new.focus_target().setFocus)
        self._update_tab(page)

    # -- broadcast input ------------------------------------------------- #
    def _broadcast_input(self, page: TabPage, source: TerminalPane, data: bytes) -> None:
        """Send what the user typed in ``source`` to every other terminal of the tab."""
        if not page.broadcast:
            return
        for pane in page.panes():
            if pane is not source and pane.state is SessionState.CONNECTED:
                pane.session.write(data)

    def set_broadcast(self, page: TabPage | None, on: bool) -> None:
        if page is None:
            return
        page.broadcast = on
        for pane in page.panes():
            pane.set_broadcast(on)
        self._update_tab(page)
        if page is self.current_page():
            self.broadcast_changed.emit(on)

    def toggle_broadcast(self, page: TabPage | None = None) -> bool:
        page = page or self.current_page()
        if page is None:
            return False
        self.set_broadcast(page, not page.broadcast)
        return page.broadcast

    def _page_of(self, pane: PaneBase) -> TabPage | None:
        for page in self.pages():
            if pane in page.items():
                return page
        return None

    def _highlight(self, page: TabPage) -> None:
        items = page.items()
        for p in items:
            p.set_active(p is page.active_item, len(items) > 1)

    def _activate(self, page: TabPage, pane: PaneBase) -> None:
        page.active_pane = pane
        self._highlight(page)
        if page is self.current_page():
            self.current_pane_changed.emit(page.active_pane)
        self._update_tab(page)

    def _pane_updated(self, page: TabPage, pane: PaneBase) -> None:
        self._update_tab(page)
        if pane is self.current_pane():
            self.pane_state_changed.emit(pane)

    def _update_tab(self, page: QWidget | None) -> None:
        if not isinstance(page, TabPage):
            return
        index = self.indexOf(page)
        if index < 0:
            return
        pane = page.active_item
        panes = page.items()
        self._highlight(page)
        title = page.custom_title or pane.title
        if len(panes) > 1:
            title += f"  [{len(panes)}]"
        if page.broadcast:
            title = "⦿ " + title
        self.setTabText(index, title)
        self.setTabIcon(index, status_dot(state_color(pane.state)))
        tip = pane.terminal_title or pane.title
        session = getattr(pane, "session", None)
        if session is not None and session.backend is not None:
            tip += f"\n{session.backend.description}"
        elif getattr(pane, "description", ""):
            tip += f"\n{pane.description}"
        if page.broadcast:
            tip += "\nBroadcast input ON: typing goes to every terminal of this tab"
        self.setTabToolTip(index, tip)

    def _current_changed(self, _index: int) -> None:
        self.current_pane_changed.emit(self.current_pane())
        page = self.current_page()
        self.broadcast_changed.emit(bool(page and page.broadcast))
        item = self.current_item()
        if item is not None:
            QTimer.singleShot(0, item.focus_target().setFocus)

    # ------------------------------------------------------------------ #
    def close_pane(self, pane: PaneBase) -> None:
        page = self._page_of(pane)
        if page is None:
            return
        if len(page.items()) <= 1:
            self.close_tab(self.indexOf(page))
            return
        if isinstance(pane, TerminalPane):
            self.closed_specs.append([pane.spec])
        pane.close_session()
        page.remove(pane)
        self._update_tab(page)
        new_active = page.active_item
        QTimer.singleShot(0, new_active.focus_target().setFocus)
        self.current_pane_changed.emit(self.current_pane())

    def close_tab(self, index: int) -> None:
        page = self.widget(index)
        if not isinstance(page, TabPage):
            return
        specs = [p.spec for p in page.panes()]
        if specs:
            self.closed_specs.append(specs)
            del self.closed_specs[:-20]
        for pane in page.items():
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
        terminal = page.active_pane
        if terminal is not None:
            menu.addAction("Reconnect", terminal.reconnect)
            menu.addAction("Duplicate", lambda: self.duplicate_requested.emit(terminal))
        bc = menu.addAction(icon("broadcast"), "Broadcast Input to All Terminals", lambda: self.toggle_broadcast(page))
        bc.setCheckable(True)
        bc.setChecked(page.broadcast)
        bc.setEnabled(len(page.panes()) > 1 or page.broadcast)
        menu.addSeparator()
        menu.addAction("Close", lambda: self.close_tab(self.indexOf(page)))
        menu.addAction("Close Other Tabs", lambda: self._close_others(page))
        menu.exec(bar.mapToGlobal(pos))

    def _close_others(self, keep: TabPage) -> None:
        for page in self.pages():
            if page is not keep:
                self.close_tab(self.indexOf(page))
