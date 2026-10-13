"""Docker tab of a server: containers (actions + logs) and images, over SSH only."""

from __future__ import annotations

import threading
from html import escape
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.i18n import tr
from ssh_terminal.services.docker_service import (
    Container,
    DockerClient,
    DockerError,
    DockerSummary,
    Image,
    Problem,
    allowed_actions,
    safe_ref,
)
from ssh_terminal.ui.dialogs import mark_primary
from ssh_terminal.ui.server_pane import ServerPane
from ssh_terminal.ui.theme import current_palette, status_dot

LOG_LINE_LIMIT = 10_000
ROLE_ITEM = Qt.ItemDataRole.UserRole + 1
ROLE_SORT = Qt.ItemDataRole.UserRole + 2
INSTALL_COMMAND = "curl -fsSL https://get.docker.com | sh"
GROUP_COMMAND = "sudo usermod -aG docker $USER   # then log out and back in"


def state_label(state: str) -> str:
    return {"running": tr("running"), "paused": tr("paused"), "exited": tr("stopped"), "created": tr("created"),
            "restarting": tr("restarting"), "removing": tr("removing"), "dead": tr("dead")}.get(state, state)


def state_color(state: str) -> str:
    pal = current_palette()
    return {"running": pal.success, "paused": pal.warning, "restarting": pal.warning}.get(state, pal.muted)


# ---------------------------------------------------------------------- #
# Models
# ---------------------------------------------------------------------- #
class ContainerModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[Container] = []

    def headers(self) -> list[str]:
        return ["#", tr("State"), tr("Name"), tr("Image"), tr("Status"), tr("Ports"), "ID"]

    def set_items(self, items: list[Container]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 7

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802, ANN401
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers()[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: ANN401
        c = self.items[index.row()]
        col = index.column()
        if role == ROLE_ITEM:
            return c
        if role == ROLE_SORT:
            return [index.row(), c.state, c.name.lower(), c.image.lower(), c.status, c.ports, c.id][col]
        if role == Qt.ItemDataRole.DisplayRole:
            return [str(index.row() + 1), state_label(c.state), c.name, c.image, c.status, c.ports, c.short_id][col]
        if role == Qt.ItemDataRole.DecorationRole and col == 1:
            return status_dot(state_color(c.state), 10)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{c.name}\n{c.image}\n{c.status}\n{c.id}"
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            return QColor(current_palette().muted)
        return None


class ContainerFilter(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.text = ""
        self.only_running = False
        self.setSortRole(ROLE_SORT)

    def update(self, text: str | None = None, only_running: bool | None = None) -> None:
        modern = hasattr(self, "beginFilterChange")  # Qt 6.10+; older versions only have invalidateFilter
        if modern:
            self.beginFilterChange()
        if text is not None:
            self.text = text.strip().lower()
        if only_running is not None:
            self.only_running = only_running
        if modern:
            self.endFilterChange()
        else:
            self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        c: Container = self.sourceModel().items[row]
        if self.only_running and c.state not in ("running", "restarting"):
            return False
        return not self.text or any(self.text in v.lower() for v in (c.name, c.image, c.id))


class ImageModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[Image] = []

    def set_items(self, items: list[Image]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 5

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802, ANN401
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return [tr("Repository"), tr("Tag"), "ID", tr("Size"), tr("Created")][section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: ANN401
        i = self.items[index.row()]
        if role == ROLE_ITEM:
            return i
        if role in (Qt.ItemDataRole.DisplayRole, ROLE_SORT):
            return [i.repository, i.tag, i.id, i.size, i.created][index.column()]
        return None


class LogHighlighter(QSyntaxHighlighter):
    """WARN lines in yellow, ERROR lines in red."""

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        upper = text.upper()
        pal = current_palette()
        color = pal.danger if ("ERROR" in upper or "FATAL" in upper or "PANIC" in upper) else \
            pal.warning if ("WARN" in upper) else None
        if color:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self.setFormat(0, len(text), fmt)


def _table(model: QAbstractTableModel | QSortFilterProxyModel) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setSortingEnabled(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setAlternatingRowColors(False)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(26)
    view.horizontalHeader().setStretchLastSection(True)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    view.setShowGrid(False)
    view.setWordWrap(False)
    return view


# ---------------------------------------------------------------------- #
class DockerPane(ServerPane):
    ICON = "container"

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        self.docker: DockerClient | None = None
        self.summary: DockerSummary | None = None
        self._busy = False
        self._busy_action = False
        self._log_stop: threading.Event | None = None
        self._log_ref = ""
        self._pull_stop: threading.Event | None = None
        super().__init__(*args, **kwargs)
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(lambda: self.refresh(quiet=True))

    def kind_title(self) -> str:
        return tr("Docker")

    # -- layout ----------------------------------------------------------------- #
    def build_content(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(8, 6, 8, 6)
        col.setSpacing(6)

        top = QHBoxLayout()
        self.heading = QLabel(tr("Docker"))
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setObjectName("SectionTitle")
        self.heading.setStyleSheet("font-size: 15px;")
        self.counts = QLabel("")
        self.counts.setTextFormat(Qt.TextFormat.PlainText)
        self.counts.setObjectName("Muted")
        top.addWidget(self.heading)
        top.addSpacing(8)
        top.addWidget(self.counts, 1)
        self.auto = QComboBox()
        for seconds in (0, 5, 10, 30, 60):
            self.auto.addItem(tr("Auto refresh: off") if not seconds else tr("Every {n} s", n=seconds), seconds)
        self.auto.currentIndexChanged.connect(self._auto_changed)
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda: self.refresh())
        top.addWidget(self.auto)
        top.addWidget(self.refresh_button)
        col.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_containers(), tr("Containers"))
        self.tabs.addTab(self._build_images(), tr("Images"))
        col.addWidget(self.tabs, 1)
        return page

    def _build_containers(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 6, 0, 0)
        col.setSpacing(6)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search by name, image or ID"))
        self.search.setClearButtonEnabled(True)
        self.only_running = QCheckBox(tr("Only running"))
        filters.addWidget(self.search, 1)
        filters.addWidget(self.only_running)
        col.addLayout(filters)

        self.model = ContainerModel()
        self.proxy = ContainerFilter()
        self.proxy.setSourceModel(self.model)
        self.search.textChanged.connect(lambda t: self.proxy.update(text=t))
        self.only_running.toggled.connect(lambda on: self.proxy.update(only_running=on))
        self.table = _table(self.proxy)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._selection_changed())
        self.table.doubleClicked.connect(lambda _i: self.show_logs())
        self.empty = QLabel(tr("No containers on this server."))
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()

        split = QSplitter(Qt.Orientation.Vertical)
        upper = QWidget()
        upper_col = QVBoxLayout(upper)
        upper_col.setContentsMargins(0, 0, 0, 0)
        upper_col.addWidget(self.table, 1)
        upper_col.addWidget(self.empty)
        split.addWidget(upper)
        split.addWidget(self._build_logs())
        split.setSizes([360, 260])
        col.addWidget(split, 1)

        bar = QHBoxLayout()
        self.action_buttons: dict[str, QPushButton] = {}
        for key, label in (("start", tr("Start")), ("stop", tr("Stop")), ("restart", tr("Restart")),
                           ("pause", tr("Pause")), ("unpause", tr("Resume")), ("remove", tr("Remove"))):
            button = QPushButton(label)
            if key == "remove":
                mark_primary(button, danger=True)
            button.clicked.connect(lambda _c=False, k=key: self.run_action(k))
            bar.addWidget(button)
            self.action_buttons[key] = button
        bar.addStretch(1)
        self.logs_button = QPushButton(tr("Container logs"))
        self.logs_button.clicked.connect(self.show_logs)
        bar.addWidget(self.logs_button)
        col.addLayout(bar)
        self._selection_changed()
        return page

    def _build_logs(self) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(4)
        row = QHBoxLayout()
        self.logs_title = QLabel(tr("Logs"))
        self.logs_title.setTextFormat(Qt.TextFormat.PlainText)
        self.logs_title.setObjectName("SectionTitle")
        row.addWidget(self.logs_title, 1)
        row.addWidget(QLabel(tr("Lines")))
        self.log_lines = QSpinBox()
        self.log_lines.setRange(10, 100_000)
        self.log_lines.setValue(300)
        self.log_lines.setSingleStep(100)
        self.log_lines.setMaximumWidth(100)
        row.addWidget(self.log_lines)
        self.log_follow = QCheckBox(tr("Follow"))
        self.log_follow.setChecked(True)
        self.log_timestamps = QCheckBox(tr("Timestamps"))
        row.addWidget(self.log_follow)
        row.addWidget(self.log_timestamps)
        self.log_stop_button = QPushButton(tr("Stop reading"))
        self.log_stop_button.setEnabled(False)
        self.log_stop_button.clicked.connect(self.stop_logs)
        row.addWidget(self.log_stop_button)
        col.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINE_LIMIT)  # older lines are dropped: memory stays bounded
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(font)
        self.log_view.setPlaceholderText(tr("Select a container and click “Container logs” (or double-click it)."))
        self._highlighter = LogHighlighter(self.log_view.document())
        col.addWidget(self.log_view, 1)
        for widget in (self.log_follow, self.log_timestamps):
            widget.toggled.connect(lambda _on: self._log_ref and self.show_logs(self._log_ref))
        return box

    def _build_images(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 6, 0, 0)
        col.setSpacing(6)
        pull = QHBoxLayout()
        self.pull_input = QLineEdit()
        self.pull_input.setPlaceholderText(tr("image:tag to pull, e.g. nginx:1.27"))
        self.pull_input.returnPressed.connect(self.pull)
        self.pull_button = mark_primary(QPushButton(tr("Pull")))
        self.pull_button.clicked.connect(self.pull)
        pull.addWidget(self.pull_input, 1)
        pull.addWidget(self.pull_button)
        col.addLayout(pull)
        self.pull_progress = QLabel("")
        self.pull_progress.setTextFormat(Qt.TextFormat.PlainText)
        self.pull_progress.setObjectName("Muted")
        self.pull_progress.hide()
        col.addWidget(self.pull_progress)
        self.image_model = ImageModel()
        image_proxy = QSortFilterProxyModel()
        image_proxy.setSourceModel(self.image_model)
        image_proxy.setSortRole(ROLE_SORT)
        self.image_table = _table(image_proxy)
        self.image_table.selectionModel().selectionChanged.connect(lambda *_: self._image_selection_changed())
        col.addWidget(self.image_table, 1)
        bar = QHBoxLayout()
        self.remove_image_button = mark_primary(QPushButton(tr("Remove")), danger=True)
        self.remove_image_button.clicked.connect(self.remove_image)
        self.prune_button = QPushButton(tr("Remove unused images"))
        self.prune_button.clicked.connect(self.prune_images)
        bar.addWidget(self.remove_image_button)
        bar.addStretch(1)
        bar.addWidget(self.prune_button)
        col.addLayout(bar)
        self._image_selection_changed()
        return page

    # -- lifecycle -------------------------------------------------------------- #
    def on_connected(self) -> None:
        sudo = self.docker.runner if self.docker is not None else None
        if sudo is not None and sudo.use_sudo:  # reconnected: keep the sudo choice of this panel
            self.runner.use_sudo, self.runner._sudo_password = sudo.use_sudo, sudo._sudo_password  # noqa: SLF001
        self.docker = DockerClient(self.runner)
        self._busy = False
        self.refresh()

    def on_closing(self) -> None:
        self.auto_timer.stop()
        self.stop_logs()
        if self._pull_stop is not None:
            self._pull_stop.set()

    def _auto_changed(self) -> None:
        seconds = self.auto.currentData() or 0
        if seconds:
            self.auto_timer.start(seconds * 1000)
        else:
            self.auto_timer.stop()

    # -- data --------------------------------------------------------------------- #
    def refresh(self, quiet: bool = False) -> None:
        if self.docker is None or self._busy or self.connection_lost():
            return
        self._busy = True
        self.refresh_button.setEnabled(False)
        if not quiet:
            self.refresh_button.setText(tr("Refreshing…"))
        self.task(self.docker.summary, self._loaded, self._load_failed, name="docker-refresh")

    def _loaded(self, summary: DockerSummary) -> None:
        self._busy = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText(tr("Refresh"))
        self.summary = summary
        selected = self.selected_container()
        self.model.set_items(summary.containers)
        self.image_model.set_items(summary.images)
        self.heading.setText(tr("Docker {version}", version=summary.version))
        self.counts.setText(tr("Containers: {total} (running {running}) · images: {images}",
                               total=len(summary.containers), running=summary.running, images=len(summary.images)))
        self.empty.setVisible(not summary.containers)
        if selected is not None:
            self._select(selected.id)
        self._selection_changed()
        self.show_content()

    def _load_failed(self, exc: BaseException) -> None:
        self._busy = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText(tr("Refresh"))
        self.handle_error(exc)

    def selected_container(self) -> Container | None:
        rows = self.table.selectionModel().selectedRows()
        return self.proxy.data(rows[0], ROLE_ITEM) if rows else None

    def _select(self, container_id: str) -> None:
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if self.proxy.data(index, ROLE_ITEM).id == container_id:
                self.table.selectRow(row)
                return

    def _selection_changed(self) -> None:
        c = self.selected_container()
        allowed = allowed_actions(c.state) if c is not None else set()
        for key, button in self.action_buttons.items():
            button.setEnabled(key in allowed and not self._busy_action)
        self.logs_button.setEnabled(c is not None)

    def _image_selection_changed(self) -> None:
        self.remove_image_button.setEnabled(bool(self.image_table.selectionModel().selectedRows()))

    # -- container actions ---------------------------------------------------------- #
    def run_action(self, action: str) -> None:
        c = self.selected_container()
        if c is None or self.docker is None:
            return
        force = volumes = False
        if action == "remove":
            answer = self._confirm_remove(c)
            if answer is None:
                return
            force, volumes = answer
        verb = {"start": tr("Starting"), "stop": tr("Stopping"), "restart": tr("Restarting"), "pause": tr("Pausing"),
                "unpause": tr("Resuming"), "remove": tr("Removing")}[action]
        self._busy_action = True
        self._selection_changed()
        self.show_toast(tr("{verb} {name}…", verb=verb, name=c.name), "info", 60_000)
        QGuiApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)

        def done(_out: object) -> None:
            QGuiApplication.restoreOverrideCursor()
            self._busy_action = False
            self.show_toast(tr("{name}: done.", name=c.name), "ok")
            self.refresh(quiet=True)

        def failed(exc: BaseException) -> None:
            QGuiApplication.restoreOverrideCursor()
            self._busy_action = False
            self._selection_changed()
            self.handle_error(exc)

        self.task(lambda: self.docker.action(action, c.id, force, volumes), done, failed, name="docker-action")

    def _confirm_remove(self, c: Container) -> tuple[bool, bool] | None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("Remove container"))
        box.setText(tr("Remove the container “{name}”?", name=c.name))
        box.setInformativeText(tr("This cannot be undone. The image is kept."))
        force = QCheckBox(tr("Force (stop it first if it is running)"))
        force.setChecked(c.running or c.state == "paused")
        volumes = QCheckBox(tr("Also remove its anonymous volumes"))
        box.setCheckBox(force)
        extra = QWidget()
        lay = QVBoxLayout(extra)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(volumes)
        box.layout().addWidget(extra, box.layout().rowCount(), 0, 1, box.layout().columnCount())
        remove = box.addButton(tr("Remove"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not remove:
            return None
        return force.isChecked(), volumes.isChecked()

    # -- logs --------------------------------------------------------------------------- #
    def show_logs(self, ref: str | bool = "") -> None:
        if not isinstance(ref, str) or not ref:
            c = self.selected_container()
            if c is None:
                return
            ref, name = c.id, c.name
        else:
            name = next((c.name for c in self.model.items if c.id == ref), ref[:12])
        if self.docker is None:
            return
        self.stop_logs()
        self._log_ref = ref
        stop = threading.Event()
        self._log_stop = stop
        follow = self.log_follow.isChecked()
        self.log_view.clear()
        self.logs_title.setText(tr("Logs: {name}", name=name))
        self.log_stop_button.setEnabled(True)
        tail, timestamps = self.log_lines.value(), self.log_timestamps.isChecked()

        def append(text: str) -> None:
            if stop.is_set():
                return
            bar = self.log_view.verticalScrollBar()
            at_end = bar.value() >= bar.maximum() - 2
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text)
            if follow and at_end:
                bar.setValue(bar.maximum())

        def ended(result: object) -> None:
            if self._log_stop is stop:
                self.log_stop_button.setEnabled(False)
                self._log_stop = None
                status = getattr(result, "status", 0)
                if status not in (0, -1):
                    err = getattr(result, "err", "").strip()
                    self.show_toast(tr("Could not read the logs: {error}", error=err or status), "error")

        self.task(lambda progress: self.docker.logs(ref, tail, follow, timestamps, progress, stop),
                  ended, self.handle_error, progress=append, name="docker-logs")

    def stop_logs(self) -> None:
        if self._log_stop is not None:
            self._log_stop.set()  # the reader closes its SSH channel, which ends `docker logs -f`
            self._log_stop = None
        if hasattr(self, "log_stop_button"):
            self.log_stop_button.setEnabled(False)

    # -- images ------------------------------------------------------------------------- #
    def selected_image(self) -> Image | None:
        rows = self.image_table.selectionModel().selectedRows()
        return self.image_table.model().data(rows[0], ROLE_ITEM) if rows else None

    def pull(self) -> None:
        if self._pull_stop is not None:  # the button reads "Cancel" while pulling
            self._pull_stop.set()
            return
        image = self.pull_input.text().strip()
        if not image or self.docker is None:
            return
        try:
            safe_ref(image)
        except ValueError:
            self.show_toast(tr("“{image}” is not a valid image name.", image=image), "error")
            return
        stop = threading.Event()
        self._pull_stop = stop
        self.pull_button.setText(tr("Cancel"))
        self.pull_progress.setText(tr("Pulling {image}…", image=image))
        self.pull_progress.show()

        def progress(text: str) -> None:
            lines = [ln.strip() for ln in text.replace("\r", "\n").splitlines() if ln.strip()]
            if lines:
                self.pull_progress.setText(lines[-1][:200])

        def finished(_r: object) -> None:
            cancelled = stop.is_set()
            self._pull_stop = None
            self.pull_button.setText(tr("Pull"))
            self.pull_progress.hide()
            if cancelled:
                self.show_toast(tr("Pull cancelled."), "warn")
            else:
                self.show_toast(tr("{image} pulled.", image=image), "ok")
                self.refresh(quiet=True)

        def failed(exc: BaseException) -> None:
            self._pull_stop = None
            self.pull_button.setText(tr("Pull"))
            self.pull_progress.hide()
            self.handle_error(exc)

        self.task(lambda p: self.docker.pull(image, p, stop), finished, failed, progress=progress, name="docker-pull")

    def remove_image(self) -> None:
        image = self.selected_image()
        if image is None or self.docker is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("Remove image"))
        box.setText(tr("Remove the image “{image}”?", image=image.ref))
        force = QCheckBox(tr("Force (also when containers use it)"))
        box.setCheckBox(force)
        remove = box.addButton(tr("Remove"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not remove:
            return
        self.task(lambda: self.docker.remove_image(image.ref, force.isChecked()),
                  lambda _r: (self.show_toast(tr("{image} removed.", image=image.ref), "ok"), self.refresh(quiet=True)),
                  self.handle_error, name="docker-rmi")

    def prune_images(self) -> None:
        if self.docker is None:
            return
        answer = QMessageBox.question(self, tr("Remove unused images"),
                                      tr("Remove every dangling image (not tagged and not used by any container)?"))
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.task(self.docker.prune_images,
                  lambda out: (self.show_toast((str(out).strip().splitlines() or [tr("Done.")])[-1], "ok"),
                               self.refresh(quiet=True)),
                  self.handle_error, name="docker-prune")

    # -- problems ------------------------------------------------------------------------ #
    def handle_error(self, exc: BaseException) -> None:
        if not isinstance(exc, DockerError):
            self.task_failed(exc)
            return
        host = escape(self.spec.title)
        detail = escape(str(exc))
        retry = (tr("Retry"), lambda: self.refresh())
        if exc.problem is Problem.NOT_INSTALLED:
            self.show_message(
                tr("<b>Docker is not installed on {host}.</b><br>Install it, then click Retry. The official "
                   "script is:<br><code>{command}</code>", host=host, command=INSTALL_COMMAND),
                [(tr("Copy install command"), lambda: self._copy(INSTALL_COMMAND)),
                 (tr("Open a terminal on this server"), lambda: self.open_terminal_requested.emit(self.spec)), retry])
        elif exc.problem is Problem.PERMISSION:
            self.show_message(
                tr("<b>Your user cannot use Docker on {host}.</b><br>It has no access to the Docker socket. "
                   "Use sudo for this panel (the password stays in memory only), or add your user to the "
                   "<code>docker</code> group:<br><code>{command}</code>", host=host, command=GROUP_COMMAND),
                [(tr("Use sudo…"), self.ask_sudo), (tr("Copy command"), lambda: self._copy(GROUP_COMMAND)), retry])
        elif exc.problem is Problem.SUDO_PASSWORD:
            self.show_message(tr("<b>sudo did not accept the password.</b><br>{error}", error=detail),
                              [(tr("Use sudo…"), self.ask_sudo), (tr("Without sudo"), self._no_sudo)])
        elif exc.problem is Problem.DAEMON_DOWN:
            self.show_message(
                tr("<b>The Docker service is not running on {host}.</b><br>Start it with "
                   "<code>systemctl start docker</code>.", host=host),
                [(tr("Start Docker"), self.start_daemon), retry])
        elif self.summary is None:
            self.show_message(f"<b>{tr('Docker error')}</b><br>{detail}", [retry])
        else:
            self.show_toast(str(exc), "error", 9000)

    def ask_sudo(self) -> None:
        if self.runner is None:
            return
        password, ok = QInputDialog.getText(
            self, tr("Use sudo"),
            tr("Password for sudo on {host}.\nKept in memory only while this panel is open. "
               "Leave it empty if sudo needs no password.", host=self.spec.title),
            QLineEdit.EchoMode.Password)
        if not ok:
            return
        self.runner.enable_sudo(password or None)
        self.description = f"{self.connection.config.endpoint} · sudo" if self.connection else "sudo"
        self.restyle()
        self.refresh()

    def _no_sudo(self) -> None:
        if self.runner is not None:
            self.runner.disable_sudo()
            self.description = self.connection.config.endpoint if self.connection else ""
            self.restyle()
        self.refresh()

    def start_daemon(self) -> None:
        if self.runner is None:
            return
        runner = self.runner

        def start() -> None:
            result = runner.run("systemctl start docker", timeout=60)
            if not result.ok:
                raise DockerError(Problem.OTHER, result.err.strip() or tr("Could not start Docker."))

        def failed(exc: BaseException) -> None:
            text = str(exc).lower()
            if "authentication" in text or "interactive" in text or "access denied" in text:
                self.show_message(tr("<b>Starting Docker needs administrator rights.</b><br>Use sudo and try again."),
                                  [(tr("Use sudo…"), self.ask_sudo), (tr("Retry"), self.start_daemon)])
            else:
                self.show_toast(str(exc), "error", 9000)

        self.task(start, lambda _r: self.refresh(), failed, name="docker-start-daemon")

    def _copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.show_toast(tr("Copied to the clipboard."), "ok")

    def focus_target(self) -> QWidget:
        return self.table if self.stack.currentWidget() is self.content else super().focus_target()

