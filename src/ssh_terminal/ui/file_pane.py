"""SFTP / local file browser pane (Termius-like).

A :class:`FilePane` lives in the same split tree as terminals, so it can be
opened in a new tab, to the right or below. Its header has a host selector
(this computer or any SSH host). Files can be dragged:

* from Windows Explorer / a Linux file manager into the pane → upload;
* from one file pane to another (local ↔ remote, remote ↔ remote) → transfer;
* inside the same pane onto a folder → move.

All file system calls run in a per-pane worker thread; every transfer opens
its own SFTP channel on the same SSH connection (no second login).
"""

from __future__ import annotations

import json
import logging
import os
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from PySide6.QtCore import QMimeData, QObject, QPoint, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDragMoveEvent, QDropEvent, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.errors import describe_exception
from ssh_terminal.models.connection import ConnectionKind, ConnectionSpec, SessionState
from ssh_terminal.services.file_systems import (
    FileEntry,
    FileSystem,
    LocalFS,
    Transfer,
    TransferCancelled,
    TransferProgress,
    human_size,
)
from ssh_terminal.ui.terminal_tabs import PaneBase, state_color
from ssh_terminal.ui.theme import current_palette, icon, status_dot
from ssh_terminal.ui.workers import run_async

log = logging.getLogger(__name__)

MIME_FILES = "application/x-sshdesk-files"
ROLE_ENTRY = Qt.ItemDataRole.UserRole + 1
_PANES: weakref.WeakValueDictionary[int, FilePane] = weakref.WeakValueDictionary()


# ---------------------------------------------------------------------- #
class _Bridge(QObject):
    done = Signal(object, object)  # callback, value
    message = Signal(str)


class SerialWorker:
    """Runs file system calls one at a time off the GUI thread."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="files")
        self.bridge = _Bridge()
        self.bridge.done.connect(lambda cb, value: cb(value) if cb else None)
        self.alive = True

    def submit(self, fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None) -> None:
        if not self.alive:
            return

        def run() -> None:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - forwarded to the GUI
                if self.alive:
                    self.bridge.done.emit(on_error, exc)
                return
            if self.alive:
                self.bridge.done.emit(on_done, result)

        self._executor.submit(run)

    def shutdown(self, final: Callable[[], None] | None = None) -> None:
        self.alive = False
        if final is not None:
            self._executor.submit(final)
        self._executor.shutdown(wait=False)


def entry_to_dict(e: FileEntry) -> dict[str, Any]:
    return {"name": e.name, "path": e.path, "size": e.size, "is_dir": e.is_dir, "is_link": e.is_link,
            "mode": e.mode, "mtime": e.mtime}


# ---------------------------------------------------------------------- #
class FileItem(QTreeWidgetItem):
    """Sorts folders first, sizes numerically."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:  # noqa: D105
        a, b = self.data(0, ROLE_ENTRY), other.data(0, ROLE_ENTRY)
        if a is None or b is None:
            return super().__lt__(other)
        if a.is_dir != b.is_dir:
            order = self.treeWidget().header().sortIndicatorOrder() if self.treeWidget() else Qt.SortOrder.AscendingOrder
            return a.is_dir if order == Qt.SortOrder.AscendingOrder else b.is_dir
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column == 1:
            return a.size < b.size
        if column == 2:
            return a.mtime < b.mtime
        return a.name.lower() < b.name.lower()


class FileList(QTreeWidget):
    """Tree view with drag & drop of files in and out."""

    dropped = Signal(object, str)  # QMimeData copy (dict), target dir
    focused = Signal()
    key_action = Signal(str)

    def __init__(self, pane: FilePane) -> None:
        super().__init__(pane)
        self.pane = pane
        self.setObjectName("FileList")
        self.setColumnCount(4)
        self.setHeaderLabels(["Name", "Size", "Modified", "Permissions"])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setIconSize(QSize(16, 16))
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 80), (2, 130), (3, 95)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.setColumnWidth(col, width)

    # -- drag out ------------------------------------------------------- #
    def mimeTypes(self) -> list[str]:  # noqa: N802
        return [MIME_FILES, "text/uri-list"]

    def supportedDropActions(self) -> Qt.DropAction:  # noqa: N802
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction

    def mimeData(self, items: list[QTreeWidgetItem]) -> QMimeData:  # noqa: N802
        entries = [it.data(0, ROLE_ENTRY) for it in items if it.data(0, ROLE_ENTRY) is not None]
        mime = QMimeData()
        payload = {"pane": id(self.pane), "entries": [entry_to_dict(e) for e in entries]}
        mime.setData(MIME_FILES, json.dumps(payload).encode())
        if self.pane.fs is not None and not self.pane.fs.remote:
            mime.setUrls([QUrl.fromLocalFile(e.path) for e in entries])  # drag to Explorer works too
        return mime

    # -- drop in -------------------------------------------------------- #
    @staticmethod
    def _acceptable(mime: QMimeData) -> bool:
        return mime.hasFormat(MIME_FILES) or (mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._acceptable(event.mimeData()) and self.pane.fs is not None:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        if self._acceptable(event.mimeData()) and self.pane.fs is not None:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if not self._acceptable(mime) or self.pane.fs is None:
            event.ignore()
            return
        target = self.pane.cwd
        item = self.itemAt(event.position().toPoint())
        entry = item.data(0, ROLE_ENTRY) if item is not None else None
        if entry is not None and entry.is_dir:
            target = entry.path
        data: dict[str, Any] = {}
        if mime.hasFormat(MIME_FILES):
            data = json.loads(bytes(mime.data(MIME_FILES)).decode())
        elif mime.hasUrls():
            data = {"local_paths": [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]}
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.dropped.emit(data, target)

    # -- misc ----------------------------------------------------------- #
    def focusInEvent(self, event) -> None:  # noqa: N802, ANN001
        super().focusInEvent(event)
        self.focused.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802, ANN001
        self.focused.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        mapping = {
            Qt.Key.Key_Delete: "delete",
            Qt.Key.Key_F2: "rename",
            Qt.Key.Key_F5: "refresh",
            Qt.Key.Key_Backspace: "up",
            Qt.Key.Key_Return: "open",
            Qt.Key.Key_Enter: "open",
        }
        if key in mapping:
            self.key_action.emit(mapping[key])
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------- #
class FilePane(PaneBase):
    """File browser card: header (host selector) + path bar + list + transfer bar."""

    def __init__(
        self,
        spec: ConnectionSpec | None,
        open_fs: Callable[[ConnectionSpec, Callable[[str], None]], FileSystem],
        hosts: Callable[[], list[str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        _PANES[id(self)] = self
        self.spec: ConnectionSpec | None = spec
        self._open_fs = open_fs
        self._hosts = hosts
        self.fs: FileSystem | None = None
        self.cwd = ""
        self._history: list[str] = []
        self._entries: list[FileEntry] = []
        self._state = SessionState.IDLE
        self.show_hidden = True
        self.active = False
        self.show_highlight = False
        self.description = ""
        self._closed = False
        self._transfers: list[Transfer] = []
        self._running: Transfer | None = None
        self.worker = SerialWorker()
        self.worker.bridge.message.connect(self._show_progress_message)

        self.setObjectName("PaneCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 2)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_toolbar())
        self.stack = QStackedWidget()
        self.list = FileList(self)
        self.list.itemDoubleClicked.connect(self._open_item)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.dropped.connect(self._dropped)
        self.list.focused.connect(lambda: self.activated.emit(self))
        self.list.key_action.connect(self._key_action)
        self.stack.addWidget(self.list)
        self.stack.addWidget(self._build_message_page())
        self.stack.addWidget(self._build_picker_page())
        layout.addWidget(self.stack, 1)
        layout.addWidget(self._build_transfer_bar())
        self.restyle()
        if spec is None:
            self._show_picker()
        else:
            self.connect_to(spec)

    # ------------------------------------------------------------------ #
    # Building blocks
    # ------------------------------------------------------------------ #
    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("PaneHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(10, 5, 6, 3)
        row.setSpacing(6)
        self.dot = QLabel()
        self.kind_icon = QLabel()
        self.host_combo = QComboBox()
        self.host_combo.setObjectName("HostCombo")
        self.host_combo.setMinimumWidth(170)
        self.host_combo.setToolTip("Where this panel browses: this computer or an SSH host (SFTP)")
        self.host_combo.activated.connect(self._host_chosen)
        self.subtitle = QLabel()
        row.addWidget(self.dot)
        row.addWidget(self.kind_icon)
        self.kind_icon.hide()  # the host selector already shows the icon
        row.addWidget(self.host_combo)
        row.addWidget(self.subtitle, 1)
        self.buttons: dict[str, QToolButton] = {}
        for key, icon_name, tip in (
            ("split_right", "split-right", "Split right: open another terminal or folder side by side"),
            ("split_down", "split-down", "Split down: open another terminal or folder below"),
            ("close", "x", "Close panel"),
        ):
            button = QToolButton()
            button.setToolTip(tip)
            button.setAutoRaise(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("icon_name", icon_name)
            button.clicked.connect(lambda _c=False, k=key: self.action_requested.emit(self, k))
            row.addWidget(button)
            self.buttons[key] = button
        return header

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("FileToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 2, 8, 6)
        row.setSpacing(4)
        self.tool: dict[str, QToolButton] = {}
        for key, icon_name, tip, slot in (
            ("up", "arrow-up", "Parent folder (Backspace)", self.go_up),
            ("home", "home", "Home folder", self.go_home),
            ("refresh", "refresh", "Refresh (F5)", self.refresh),
        ):
            button = QToolButton()
            button.setToolTip(tip)
            button.setProperty("icon_name", icon_name)
            button.clicked.connect(slot)
            row.addWidget(button)
            self.tool[key] = button
        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("PathEdit")
        self.path_edit.setPlaceholderText("path")
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text().strip()))
        row.addWidget(self.path_edit, 1)
        for key, icon_name, tip, slot in (
            ("mkdir", "folder-plus", "New folder", self.new_folder),
            ("upload", "upload", "Send files from this computer to this folder (or drag them here)", self.upload_dialog),
        ):
            button = QToolButton()
            button.setToolTip(tip)
            button.setProperty("icon_name", icon_name)
            button.clicked.connect(slot)
            row.addWidget(button)
            self.tool[key] = button
        return bar

    def _build_message_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.addStretch(1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.message)
        self.retry = QPushButton("Retry")
        self.retry.clicked.connect(lambda: self.spec and self.connect_to(self.spec))
        col.addWidget(self.retry, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addStretch(2)
        return page

    def _build_picker_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(14, 10, 14, 14)
        title = QLabel("Select a host")
        title.setObjectName("PickerTitle")
        col.addWidget(title)
        self.picker_search = QLineEdit()
        self.picker_search.setPlaceholderText("Search hosts…")
        self.picker_search.textChanged.connect(self._fill_picker)
        col.addWidget(self.picker_search)
        self.picker = QListWidget()
        self.picker.setObjectName("HostPicker")
        self.picker.itemActivated.connect(self._picker_chosen)
        self.picker.itemClicked.connect(self._picker_chosen)
        col.addWidget(self.picker, 1)
        return page

    def _build_transfer_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TransferBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 4, 8, 6)
        self.transfer_label = QLabel()
        self.transfer_label.setMinimumWidth(80)
        self.transfer_bar = QProgressBar()
        self.transfer_bar.setTextVisible(False)
        self.transfer_bar.setFixedHeight(6)
        self.transfer_cancel = QToolButton()
        self.transfer_cancel.setToolTip("Cancel transfer")
        self.transfer_cancel.setProperty("icon_name", "x")
        self.transfer_cancel.clicked.connect(self.cancel_transfers)
        row.addWidget(self.transfer_label, 1)
        row.addWidget(self.transfer_bar, 1)
        row.addWidget(self.transfer_cancel)
        self.transfer_widget = bar
        bar.hide()
        return bar

    # ------------------------------------------------------------------ #
    # PaneBase API
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def title(self) -> str:
        if self.spec is None:
            return "Files"
        if self.spec.kind is ConnectionKind.LOCAL:
            return "Local files"
        return f"SFTP · {self.spec.title}"

    def focus_target(self) -> QWidget:
        return self.list if self.stack.currentWidget() is self.list else self.picker

    def close_session(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_transfers()
        fs, self.fs = self.fs, None
        self.worker.shutdown(final=(fs.close if fs is not None else None))

    def set_active(self, active: bool, highlight: bool) -> None:
        if (active, highlight) != (self.active, self.show_highlight):
            self.active, self.show_highlight = active, highlight
            self.restyle()

    def restyle(self) -> None:
        pal = current_palette()
        border = pal.accent if (self.active and self.show_highlight) else pal.border
        self.setStyleSheet(
            f"#PaneCard {{ background: {pal.surface}; border: 1px solid {border}; border-radius: 9px; }}"
            f"#PaneHeader, #FileToolbar, #TransferBar {{ background: transparent; }}"
            f"#PaneHeader QToolButton:hover, #FileToolbar QToolButton:hover {{ background: rgba(127,127,127,0.18); }}"
            f"#FileList {{ background: {pal.surface}; border: none; border-top: 1px solid {pal.border}; }}"
            f"#FileList::item {{ padding: 3px 2px; }}"
            f"#PickerTitle {{ color: {pal.text}; font-weight: 600; font-size: 14px; }}"
            f"QLabel#Subtitle {{ color: {pal.muted}; }}"
        )
        self.subtitle.setObjectName("Subtitle")
        self.subtitle.setStyleSheet(f"color: {pal.muted};")
        self.dot.setPixmap(status_dot(state_color(self._state), 10).pixmap(10, 10))
        kind = "folder" if self.spec is None or self.spec.kind is ConnectionKind.LOCAL else "server"
        self.kind_icon.setPixmap(icon(kind, pal.accent).pixmap(14, 14))
        for button in [*self.buttons.values(), *self.tool.values(), self.transfer_cancel]:
            button.setIcon(icon(button.property("icon_name"), pal.muted))
            button.setIconSize(QSize(13, 13) if button in self.buttons.values() else QSize(15, 15))
        self._fill_host_combo()

    # ------------------------------------------------------------------ #
    # Host selection / connection
    # ------------------------------------------------------------------ #
    def _fill_host_combo(self) -> None:
        combo = self.host_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(icon("terminal"), "This computer", ConnectionSpec.local().to_dict())
        for alias in self._hosts():
            combo.addItem(icon("server"), alias, ConnectionSpec.ssh(alias).to_dict())
        if self.spec is None:
            combo.insertItem(0, "Select host…", None)
            combo.setCurrentIndex(0)
        elif self.spec.kind is ConnectionKind.LOCAL:
            combo.setCurrentIndex(0)
        else:
            index = combo.findText(self.spec.title)
            if index < 0:
                combo.addItem(icon("server"), self.spec.title, self.spec.to_dict())
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _host_chosen(self, index: int) -> None:
        data = self.host_combo.itemData(index)
        if data:
            self.connect_to(ConnectionSpec.from_dict(data))

    def _fill_picker(self) -> None:
        text = self.picker_search.text().strip().lower()
        self.picker.clear()
        entries = [("This computer", ConnectionSpec.local(), "terminal")]
        entries += [(alias, ConnectionSpec.ssh(alias), "server") for alias in self._hosts()]
        for label, spec, icon_name in entries:
            if text and text not in label.lower():
                continue
            item = QListWidgetItem(icon(icon_name, current_palette().accent), label)
            item.setData(ROLE_ENTRY, spec.to_dict())
            self.picker.addItem(item)

    def _show_picker(self) -> None:
        self._fill_picker()
        self.stack.setCurrentIndex(2)
        self.subtitle.setText("")
        self._set_state(SessionState.IDLE)

    def _picker_chosen(self, item: QListWidgetItem) -> None:
        self.connect_to(ConnectionSpec.from_dict(item.data(ROLE_ENTRY)))

    def _set_state(self, state: SessionState) -> None:
        self._state = state
        self.dot.setPixmap(status_dot(state_color(state), 10).pixmap(10, 10))
        self.state_changed.emit(self)

    def _show_message(self, text: str, retry: bool = False) -> None:
        self.message.setText(text)
        self.retry.setVisible(retry)
        self.stack.setCurrentIndex(1)

    def _show_progress_message(self, text: str) -> None:
        if self._state is SessionState.CONNECTING:
            self._show_message(f"Connecting to <b>{self.spec.title if self.spec else ''}</b>…<br>{text}")

    def connect_to(self, spec: ConnectionSpec) -> None:
        if self._closed:
            return
        old, self.fs = self.fs, None
        if old is not None:
            self.worker.submit(old.close)
        self.spec = ConnectionSpec(spec.kind, alias=spec.alias, shell=spec.shell, adhoc=dict(spec.adhoc))
        self._entries = []
        self.list.clear()
        self._history.clear()
        self.restyle()
        self.title_changed.emit(self)
        self._set_state(SessionState.CONNECTING)
        self._show_message(f"Connecting to <b>{self.spec.title}</b>…")
        emit = self.worker.bridge.message.emit
        self.worker.submit(lambda: self._open_fs(self.spec, emit), self._connected, self._connect_failed)

    def _connected(self, fs: FileSystem) -> None:
        if self._closed:
            fs.close()
            return
        self.fs = fs
        self._set_state(SessionState.CONNECTED)
        self.stack.setCurrentIndex(0)
        self.go_home()

    def _connect_failed(self, exc: BaseException) -> None:
        friendly = describe_exception(exc, self.spec.title if self.spec else "")
        self._set_state(SessionState.FAILED)
        first = friendly.message.splitlines()[0] if friendly.message else ""
        self._show_message(f"<b>{friendly.title}</b><br>{first}", retry=True)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def navigate(self, path: str, remember: bool = True) -> None:
        if self.fs is None:
            return
        fs = self.fs
        self.worker.submit(lambda: (path, fs.listdir(path)),
                           lambda result: self._listed(*result, remember=remember),
                           self._list_failed)

    def _listed(self, path: str, entries: list[FileEntry], remember: bool = True) -> None:
        if remember and self.cwd and self.cwd != path:
            self._history.append(self.cwd)
        self.cwd = path
        self._entries = entries
        self.path_edit.setText(path)
        self.description = f"{self.fs.label if self.fs else ''}:{path}"
        self.subtitle.setText(path if len(path) < 48 else "…" + path[-46:])
        self._populate()
        self.title_changed.emit(self)

    def _list_failed(self, exc: BaseException) -> None:
        if self.fs is not None and not self.fs.remote and not isinstance(exc, OSError):
            log.warning("listdir failed: %s", exc)
        self._error("Cannot open folder", exc)
        self.path_edit.setText(self.cwd)
        if self.spec is not None and self.spec.kind is ConnectionKind.SSH and not self._transport_alive():
            self._set_state(SessionState.DISCONNECTED)
            self._show_message("<b>Connection lost</b>", retry=True)

    def _transport_alive(self) -> bool:
        conn = getattr(self.fs, "connection", None)
        return conn is None or conn.transport.is_active()

    def _populate(self) -> None:
        pal = current_palette()
        folder_icon, file_icon = icon("folder", pal.accent), icon("file", pal.muted)
        self.list.setSortingEnabled(False)
        self.list.clear()
        for entry in self._entries:
            if not self.show_hidden and entry.name.startswith("."):
                continue
            modified = datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M") if entry.mtime else ""
            item = FileItem([entry.name, "" if entry.is_dir else human_size(entry.size), modified, entry.permissions])
            item.setIcon(0, folder_icon if entry.is_dir else file_icon)
            item.setData(0, ROLE_ENTRY, entry)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setForeground(2, self.palette().placeholderText())
            item.setForeground(3, self.palette().placeholderText())
            flags = item.flags() | Qt.ItemFlag.ItemIsDragEnabled
            if entry.is_dir:
                flags |= Qt.ItemFlag.ItemIsDropEnabled
            item.setFlags(flags)
            self.list.addTopLevelItem(item)
        self.list.setSortingEnabled(True)

    def refresh(self) -> None:
        if self.fs is not None:
            self.navigate(self.cwd, remember=False)
        elif self.spec is not None and self._state in (SessionState.FAILED, SessionState.DISCONNECTED):
            self.connect_to(self.spec)

    def go_up(self) -> None:
        if self.fs is not None and self.cwd:
            self.navigate(self.fs.parent(self.cwd))

    def go_home(self) -> None:
        if self.fs is None:
            return
        fs = self.fs
        self.worker.submit(lambda: (h := fs.home(), fs.listdir(h)), lambda r: self._listed(*r), self._list_failed)

    def go_back(self) -> None:
        if self._history:
            self.navigate(self._history.pop(), remember=False)

    def selected_entries(self) -> list[FileEntry]:
        return [it.data(0, ROLE_ENTRY) for it in self.list.selectedItems() if it.data(0, ROLE_ENTRY) is not None]

    def _open_item(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        entry: FileEntry | None = item.data(0, ROLE_ENTRY)
        if entry is None:
            return
        if entry.is_dir:
            self.navigate(entry.path)
        elif self.fs is not None and not self.fs.remote:
            QDesktopServices.openUrl(QUrl.fromLocalFile(entry.path))
        else:
            other = self.other_pane(local_only=True)
            if other is not None:
                other.start_transfer(self, [entry], other.cwd)

    def _key_action(self, action: str) -> None:
        if action == "delete":
            self.delete_selected()
        elif action == "rename":
            self.rename_selected()
        elif action == "refresh":
            self.refresh()
        elif action == "up":
            self.go_up()
        elif action == "open":
            items = self.list.selectedItems()
            if len(items) == 1:
                self._open_item(items[0])

    # ------------------------------------------------------------------ #
    # File operations
    # ------------------------------------------------------------------ #
    def _error(self, title: str, exc: BaseException) -> None:
        if self._closed:
            return
        friendly = describe_exception(exc)
        text = str(exc) or friendly.message
        QMessageBox.warning(self, title, text)

    def new_folder(self) -> None:
        if self.fs is None:
            return
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name.strip():
            fs, path = self.fs, self.fs.join(self.cwd, name.strip())
            self.worker.submit(lambda: fs.mkdir(path), lambda _r: self.refresh(), lambda e: self._error("New folder", e))

    def rename_selected(self) -> None:
        entries = self.selected_entries()
        if self.fs is None or len(entries) != 1:
            return
        entry = entries[0]
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry.name)
        if ok and name.strip() and name.strip() != entry.name:
            fs, new = self.fs, self.fs.join(self.fs.parent(entry.path), name.strip())
            self.worker.submit(lambda: fs.rename(entry.path, new), lambda _r: self.refresh(), lambda e: self._error("Rename", e))

    def delete_selected(self) -> None:
        entries = self.selected_entries()
        if self.fs is None or not entries:
            return
        what = f"“{entries[0].name}”" if len(entries) == 1 else f"{len(entries)} items"
        where = "this computer" if not self.fs.remote else self.fs.label
        answer = QMessageBox.question(self, "Delete", f"Delete {what} from {where}?\nThis cannot be undone.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        fs = self.fs

        def run() -> None:
            for e in entries:
                fs.remove(e)

        self.worker.submit(run, lambda _r: self.refresh(), lambda e: (self._error("Delete", e), self.refresh()))

    def copy_paths(self) -> None:
        paths = [e.path for e in self.selected_entries()] or [self.cwd]
        QApplication.clipboard().setText("\n".join(paths))

    def upload_dialog(self) -> None:
        if self.fs is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Send files to this folder")
        if paths:
            self._transfer_local_paths(paths, self.cwd)

    def download_dialog(self) -> None:
        entries = self.selected_entries()
        if self.fs is None or not entries:
            return
        folder = QFileDialog.getExistingDirectory(self, "Download to folder", os.path.expanduser("~"))
        if folder:
            self._queue(Transfer(self.fs, entries, LocalFS(), folder), refresh=[self.other_pane(local_only=True)])

    # ------------------------------------------------------------------ #
    # Drag & drop / transfers
    # ------------------------------------------------------------------ #
    def tab_page(self) -> QWidget | None:
        w = self.parentWidget()
        while w is not None and not hasattr(w, "items"):
            w = w.parentWidget()
        return w

    def other_pane(self, local_only: bool = False) -> FilePane | None:
        page = self.tab_page()
        if page is None:
            return None
        for item in page.items():
            if isinstance(item, FilePane) and item is not self and item.fs is not None:
                if not local_only or not item.fs.remote:
                    return item
        return None

    def _dropped(self, data: dict[str, Any], target: str) -> None:
        if self.fs is None:
            return
        if "local_paths" in data:
            self._transfer_local_paths(data["local_paths"], target)
            return
        source = _PANES.get(data.get("pane", 0))
        entries = [FileEntry(**d) for d in data.get("entries", [])]
        if source is None or source.fs is None or not entries:
            return
        if source is self:
            # inside the same panel: move into the folder it was dropped on
            moving = [e for e in entries if e.path != target and self.fs.parent(e.path) != target]
            if moving and target != self.cwd:
                fs = self.fs

                def run() -> None:
                    for e in moving:
                        fs.rename(e.path, fs.join(target, e.name))

                self.worker.submit(run, lambda _r: self.refresh(), lambda e: self._error("Move", e))
            return
        self.start_transfer(source, entries, target)

    def _transfer_local_paths(self, paths: list[str], target: str) -> None:
        local = LocalFS()
        entries = []
        for p in paths:
            try:
                entries.append(local.stat(os.path.normpath(p)))
            except OSError as exc:
                self._error("Cannot read file", exc)
        if entries:
            self._queue(Transfer(local, entries, self.fs, target), refresh=[self])

    def start_transfer(self, source: FilePane, entries: list[FileEntry], target: str) -> None:
        """Copy ``entries`` from ``source`` into ``target`` of this pane."""
        if self.fs is None or source.fs is None:
            return
        self._queue(Transfer(source.fs, entries, self.fs, target), refresh=[self])

    def _queue(self, transfer: Transfer, refresh: list[FilePane | None]) -> None:
        if transfer.dest_dir == self.cwd and transfer.dst is self.fs:
            existing = {e.name for e in self._entries}
            clash = [e.name for e in transfer.entries if e.name in existing]
            if clash:
                names = ", ".join(clash[:4]) + ("…" if len(clash) > 4 else "")
                box = QMessageBox(QMessageBox.Icon.Question, "Replace files?",
                                  f"{len(clash)} item(s) already exist here: {names}", parent=self)
                replace = box.addButton("Replace", QMessageBox.ButtonRole.AcceptRole)
                skip = box.addButton("Skip", QMessageBox.ButtonRole.ActionRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                if box.clickedButton() is skip:
                    transfer.skip = set(clash)
                elif box.clickedButton() is not replace:
                    return
        transfer.refresh = [p for p in refresh if p is not None]  # type: ignore[attr-defined]
        self._transfers.append(transfer)
        self._next_transfer()

    def _next_transfer(self) -> None:
        if self._running is not None or not self._transfers or self._closed:
            return
        transfer = self._transfers.pop(0)
        self._running = transfer
        verb = "Uploading" if transfer.dst.remote and not transfer.src.remote else (
            "Downloading" if transfer.src.remote and not transfer.dst.remote else "Copying")
        count = len(transfer.entries)
        self._transfer_title = f"{verb} {count} item{'s' if count != 1 else ''} → {transfer.dest_dir}"
        self.transfer_label.setText(self._transfer_title)
        self.transfer_bar.setRange(0, 0)
        self.transfer_widget.show()
        run_async(transfer.run, pass_progress=True, on_progress=self._transfer_progress,
                  on_done=lambda _p, t=transfer: self._transfer_done(t, None),
                  on_error=lambda e, t=transfer: self._transfer_done(t, e), name="sftp-transfer")

    def _transfer_progress(self, progress: TransferProgress) -> None:
        if self._closed:
            return
        if progress.total > 0:
            self.transfer_bar.setRange(0, 1000)
            self.transfer_bar.setValue(int(progress.done * 1000 / progress.total))
            pct = int(progress.done * 100 / progress.total)
            self.transfer_label.setText(
                f"{self._transfer_title} — {pct}% ({human_size(progress.done)} of {human_size(progress.total)})"
            )

    def _transfer_done(self, transfer: Transfer, error: BaseException | None) -> None:
        self._running = None
        if self._closed:
            return
        for pane in getattr(transfer, "refresh", []):
            if pane.fs is not None and not pane._closed:
                pane.refresh()
        if error is not None and not isinstance(error, TransferCancelled):
            self._error("Transfer failed", error)
        if self._transfers:
            self._next_transfer()
        else:
            self.transfer_widget.hide()

    def cancel_transfers(self) -> None:
        for t in self._transfers:
            t.cancel_event.set()
        self._transfers.clear()
        if self._running is not None:
            self._running.cancel_event.set()

    # ------------------------------------------------------------------ #
    def _context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        entries = self.selected_entries()
        if self.fs is None:
            return
        other = self.other_pane()
        if len(entries) == 1 and entries[0].is_dir:
            menu.addAction(icon("folder"), "Open", lambda: self.navigate(entries[0].path))
        if entries:
            if other is not None:
                target = "this computer" if not other.fs.remote else other.fs.label
                menu.addAction(icon("download" if self.fs.remote else "upload"),
                               f"Copy to {target} ({other.cwd})",
                               lambda: other.start_transfer(self, entries, other.cwd))
            if self.fs.remote:
                menu.addAction(icon("download"), "Download to…", self.download_dialog)
            menu.addSeparator()
            rename = menu.addAction("Rename", self.rename_selected)
            rename.setEnabled(len(entries) == 1)
            menu.addAction("Delete", self.delete_selected)
            menu.addSeparator()
        menu.addAction(icon("upload"), "Send Files Here…", self.upload_dialog)
        menu.addAction(icon("folder-plus"), "New Folder…", self.new_folder)
        menu.addAction("Copy Path", self.copy_paths)
        hidden = menu.addAction("Show Hidden Files")
        hidden.setCheckable(True)
        hidden.setChecked(self.show_hidden)
        hidden.toggled.connect(self._toggle_hidden)
        menu.addAction(icon("refresh"), "Refresh", self.refresh)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _toggle_hidden(self, on: bool) -> None:
        self.show_hidden = on
        self._populate()
