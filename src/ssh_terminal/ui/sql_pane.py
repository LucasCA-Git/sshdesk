"""SQL console of a server: MySQL/MariaDB or PostgreSQL through an SSH tunnel."""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, QStringListModel, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QKeyEvent,
    QKeySequence,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.i18n import tr
from ssh_terminal.services.docker_service import DatabaseCandidate, DockerClient, database_candidates
from ssh_terminal.services.sql_service import (
    DEFAULT_MAX_ROWS,
    KINDS,
    HistoryEntry,
    HistoryStore,
    ProfileStore,
    QueryCancelled,
    QueryResult,
    SqlError,
    SqlProfile,
    SqlSession,
    SqlTunnel,
    destructive_reason,
    now_text,
    open_session,
    split_statements,
    statement_at,
)
from ssh_terminal.ui.dialogs import mark_primary
from ssh_terminal.ui.server_pane import ServerPane
from ssh_terminal.ui.theme import current_palette
from ssh_terminal.utils.paths import get_app_dir

KEYWORDS = (
    "SELECT FROM WHERE AND OR NOT NULL IS IN LIKE ILIKE BETWEEN ORDER BY GROUP HAVING LIMIT OFFSET JOIN LEFT RIGHT "
    "INNER OUTER FULL CROSS ON AS DISTINCT UNION ALL EXISTS CASE WHEN THEN ELSE END INSERT INTO VALUES UPDATE SET "
    "DELETE CREATE TABLE VIEW INDEX DROP ALTER ADD COLUMN PRIMARY KEY FOREIGN REFERENCES DEFAULT TRUNCATE SHOW "
    "DESCRIBE EXPLAIN ANALYZE WITH RETURNING COUNT SUM AVG MIN MAX COALESCE CAST USE DATABASE SCHEMA BEGIN COMMIT "
    "ROLLBACK GRANT REVOKE ASC DESC TRUE FALSE"
).split()
ROLE_ROW = Qt.ItemDataRole.UserRole + 1


# ---------------------------------------------------------------------- #
# Editor
# ---------------------------------------------------------------------- #
class SqlHighlighter(QSyntaxHighlighter):
    _word = re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.IGNORECASE)
    _string = re.compile(r"'(?:[^'\\]|\\.|'')*'?")
    _number = re.compile(r"\b\d+(?:\.\d+)?\b")
    _comment = re.compile(r"--[^\n]*|#[^\n]*")

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        pal = current_palette()

        def fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.DemiBold)
            f.setFontItalic(italic)
            return f

        for m in self._word.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), fmt(pal.accent, bold=True))
        for m in self._number.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), fmt(pal.warning))
        for m in self._string.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), fmt(pal.success))
        for m in self._comment.finditer(text):
            self.setFormat(m.start(), len(text) - m.start(), fmt(pal.muted, italic=True))
        # /* block comments */ across lines
        start = 0 if self.previousBlockState() == 1 else text.find("/*")
        while start >= 0:
            end = text.find("*/", start + 2)
            if end < 0:
                self.setCurrentBlockState(1)
                self.setFormat(start, len(text) - start, fmt(pal.muted, italic=True))
                break
            self.setFormat(start, end + 2 - start, fmt(pal.muted, italic=True))
            start = text.find("/*", end + 2)
        else:
            self.setCurrentBlockState(0)


class SqlEditor(QPlainTextEdit):
    """Plain text editor with SQL colors, Ctrl+Enter to run and keyword/table/column completion."""

    run_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSizeF(max(10.0, font.pointSizeF()))
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setPlaceholderText(tr("Type SQL here. Ctrl+Enter runs the selection or the statement under the cursor."))
        self._highlighter = SqlHighlighter(self.document())
        self.words = QStringListModel(KEYWORDS)
        self.completer = QCompleter(self.words, self)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.activated.connect(self._insert_completion)

    def set_names(self, names: list[str]) -> None:
        self.words.setStringList(sorted(set(KEYWORDS) | set(names), key=str.lower))

    def _word_before_cursor(self) -> str:
        cursor = self.textCursor()
        text = cursor.block().text()[: cursor.positionInBlock()]
        m = re.search(r"[A-Za-z_][A-Za-z0-9_.]*$", text)
        return m.group(0) if m else ""

    def _insert_completion(self, word: str) -> None:
        prefix = self._word_before_cursor()
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, len(prefix))
        cursor.insertText(word)
        self.setTextCursor(cursor)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        popup = self.completer.popup()
        if popup.isVisible() and event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Tab,
                                                 Qt.Key.Key_Escape):
            if event.key() == Qt.Key.Key_Escape:
                popup.hide()
            else:
                index = popup.currentIndex()
                if index.isValid():
                    self._insert_completion(index.data())
                popup.hide()
            return
        ctrl = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        if ctrl and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.run_requested.emit()
            return
        if ctrl and event.key() == Qt.Key.Key_Space:
            self._complete(force=True)
            return
        super().keyPressEvent(event)
        if event.text() and (event.text().isalnum() or event.text() in "_."):
            self._complete()
        elif popup.isVisible():
            popup.hide()

    def _complete(self, force: bool = False) -> None:
        prefix = self._word_before_cursor()
        if len(prefix) < (1 if force else 2):
            self.completer.popup().hide()
            return
        self.completer.setCompletionPrefix(prefix.rsplit(".", 1)[-1] if "." in prefix else prefix)
        if self.completer.completionCount() == 0:
            self.completer.popup().hide()
            return
        rect = self.cursorRect()
        rect.setWidth(260)
        self.completer.complete(rect)


# ---------------------------------------------------------------------- #
# Results
# ---------------------------------------------------------------------- #
def cell_text(value: Any) -> str:  # noqa: ANN401
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return f"0x{data[:64].hex()}" + ("…" if len(data) > 64 else "")
    if isinstance(value, float):
        return repr(value)
    return str(value)


def json_value(value: Any) -> Any:  # noqa: ANN401
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


class ResultModel(QAbstractTableModel):
    """Read-only grid; Qt's views only ask for visible rows, so thousands of rows stay fast."""

    def __init__(self) -> None:
        super().__init__()
        self.columns: list[str] = []
        self.rows: list[tuple] = []

    def set_result(self, columns: list[str], rows: list[tuple]) -> None:
        self.beginResetModel()
        self.columns, self.rows = columns, rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802, ANN401
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.columns[section] if orientation == Qt.Orientation.Horizontal else str(section + 1)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: ANN401
        value = self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            text = cell_text(value)
            return text if len(text) <= 500 else text[:500] + "…"
        if role == Qt.ItemDataRole.ToolTipRole and value is not None:
            text = cell_text(value)
            return text[:2000] if len(text) > 60 else None
        if role == Qt.ItemDataRole.EditRole:  # used for sorting: real values, NULL first
            return (value is not None, value if isinstance(value, (int, float, Decimal)) else cell_text(value))
        if value is None and role == Qt.ItemDataRole.ForegroundRole:
            return QColor(current_palette().muted)
        if value is None and role == Qt.ItemDataRole.FontRole:
            font = QFont()
            font.setItalic(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole and isinstance(value, (int, float, Decimal)):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class ResultSort(QSortFilterProxyModel):
    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        a = self.sourceModel().data(left, Qt.ItemDataRole.EditRole)
        b = self.sourceModel().data(right, Qt.ItemDataRole.EditRole)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


# ---------------------------------------------------------------------- #
# Connection dialog
# ---------------------------------------------------------------------- #
class SqlConnectDialog(QDialog):
    def __init__(self, host: str, profiles: list[SqlProfile], candidates: list[DatabaseCandidate],
                 password_for, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle(tr("Connect to a database on {host}", host=host))
        self.setMinimumWidth(460)
        self._password_for = password_for
        layout = QVBoxLayout(self)
        intro = QLabel(tr("The database does not need to be exposed: VeyLix opens an SSH tunnel to it. "
                          "Use the address as seen from the server (usually 127.0.0.1)."))
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        layout.addWidget(intro)
        form = QFormLayout()
        self.saved = QComboBox()
        self.saved.addItem(tr("New connection"), None)
        for p in profiles:
            self.saved.addItem(p.label() + (f" · {p.database}" if p.database else ""), p)
        for c in candidates:
            self.saved.addItem(tr("Docker: {where}", where=c.describe()), c)
        self.saved.currentIndexChanged.connect(self._pick)
        if self.saved.count() > 1:
            form.addRow(tr("Saved / detected"), self.saved)
        self.kind = QComboBox()
        for key, (label, _port) in KINDS.items():
            self.kind.addItem(label, key)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.host = QLineEdit("127.0.0.1")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(3306)
        self.user = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.database = QLineEdit()
        self.database.setPlaceholderText(tr("optional"))
        self.remember = QCheckBox(tr("Remember (password encrypted in the system keyring / local vault)"))
        self.read_only = QCheckBox(tr("Read-only: only SELECT / SHOW / EXPLAIN"))
        form.addRow(tr("Type"), self.kind)
        form.addRow(tr("Remote host"), self.host)
        form.addRow(tr("Port"), self.port)
        form.addRow(tr("User"), self.user)
        form.addRow(tr("Password"), self.password)
        form.addRow(tr("Default database"), self.database)
        form.addRow("", self.remember)
        form.addRow("", self.read_only)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        ok = mark_primary(QPushButton(tr("Connect")))
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
        if profiles:
            self.saved.setCurrentIndex(1)

    def _kind_changed(self) -> None:
        defaults = {port for _label, port in KINDS.values()}
        if self.port.value() in defaults:
            self.port.setValue(KINDS[self.kind.currentData()][1])

    def _pick(self) -> None:
        data = self.saved.currentData()
        if isinstance(data, SqlProfile):
            self.kind.setCurrentIndex(self.kind.findData(data.kind))
            self.host.setText(data.host)
            self.port.setValue(data.port)
            self.user.setText(data.user)
            self.database.setText(data.database)
            self.read_only.setChecked(data.read_only)
            self.remember.setChecked(True)
            self.password.setText(self._password_for(data) or "")
        elif isinstance(data, DatabaseCandidate):
            self.kind.setCurrentIndex(self.kind.findData(data.kind))
            self.host.setText(data.host)
            self.port.setValue(data.port)
            if not self.user.text():
                self.user.setText("postgres" if data.kind == "postgres" else "root")
            self.password.setFocus()

    def _accept(self) -> None:
        if not self.user.text().strip():
            self.user.setFocus()
            return
        self.accept()

    def profile(self) -> SqlProfile:
        return SqlProfile(kind=self.kind.currentData(), host=self.host.text().strip() or "127.0.0.1",
                          port=self.port.value(), user=self.user.text().strip(), database=self.database.text().strip(),
                          read_only=self.read_only.isChecked(), remember=self.remember.isChecked())


# ---------------------------------------------------------------------- #
class _DbBridge(QObject):
    done = Signal(object, object, int)  # callback, value, token


class SqlPane(ServerPane):
    ICON = "database"

    def __init__(self, spec, open_ssh, credentials=None, app_dir: Path | None = None,  # noqa: ANN001
                 parent: QWidget | None = None) -> None:
        self.credentials = credentials
        base = app_dir or get_app_dir()
        self.profiles = ProfileStore(base / "sql_connections.json")
        self.history = HistoryStore(base / "sql_history.json")
        self.session: SqlSession | None = None
        self.tunnel: SqlTunnel | None = None
        self.profile: SqlProfile | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sql")  # one DB call at a time
        self._bridge = _DbBridge()
        self._bridge.done.connect(self._deliver)
        self._token = 0
        self._running = False
        self._asked = False
        super().__init__(spec, open_ssh, parent)

    def kind_title(self) -> str:
        return tr("SQL")

    # -- layout --------------------------------------------------------------- #
    def build_content(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(8, 6, 8, 6)
        col.setSpacing(6)
        top = QHBoxLayout()
        heading = QLabel(tr("SQL console"))
        heading.setObjectName("SectionTitle")
        heading.setStyleSheet("font-size: 15px;")
        self.info = QLabel(tr("Not connected"))
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        self.info.setObjectName("Muted")
        top.addWidget(heading)
        top.addSpacing(8)
        top.addWidget(self.info, 1)
        self.other_button = QPushButton(tr("Another connection"))
        self.other_button.clicked.connect(self.ask_connection)
        self.disconnect_button = QPushButton(tr("Disconnect"))
        self.disconnect_button.clicked.connect(self.disconnect_db)
        top.addWidget(self.other_button)
        top.addWidget(self.disconnect_button)
        col.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_schema())
        split.addWidget(self._build_work())
        split.setSizes([220, 760])
        col.addWidget(split, 1)
        self._set_db_enabled(False)
        return page

    def _build_schema(self) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        self.db_combo = QComboBox()
        self.db_combo.setToolTip(tr("Database / schema"))
        self.db_combo.activated.connect(self._database_chosen)
        self.table_filter = QLineEdit()
        self.table_filter.setPlaceholderText(tr("Filter tables"))
        self.table_filter.setClearButtonEnabled(True)
        self.table_filter.textChanged.connect(self._filter_tables)
        self.table_list = QListWidget()
        self.table_list.itemClicked.connect(lambda item: self.preview_table(item.text()))
        self.table_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_list.customContextMenuRequested.connect(self._table_menu)
        col.addWidget(self.db_combo)
        col.addWidget(self.table_filter)
        col.addWidget(self.table_list, 1)
        return box

    def _build_work(self) -> QWidget:
        split = QSplitter(Qt.Orientation.Vertical)
        editor_box = QWidget()
        col = QVBoxLayout(editor_box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        self.editor = SqlEditor()
        self.editor.run_requested.connect(self.run)
        col.addWidget(self.editor, 1)
        bar = QHBoxLayout()
        self.run_button = mark_primary(QPushButton(tr("Run (Ctrl+Enter)")))
        self.run_button.clicked.connect(self.run)
        self.cancel_button = QPushButton(tr("Cancel query"))
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        bar.addWidget(self.run_button)
        bar.addWidget(self.cancel_button)
        bar.addSpacing(10)
        bar.addWidget(QLabel(tr("Max rows")))
        self.max_rows = QSpinBox()
        self.max_rows.setRange(1, 1_000_000)
        self.max_rows.setValue(DEFAULT_MAX_ROWS)
        self.max_rows.setSingleStep(1000)
        self.max_rows.setMaximumWidth(110)
        bar.addWidget(self.max_rows)
        bar.addWidget(QLabel(tr("Timeout (s)")))
        self.timeout = QSpinBox()
        self.timeout.setRange(0, 86_400)
        self.timeout.setValue(0)
        self.timeout.setSpecialValueText(tr("none"))
        self.timeout.setMaximumWidth(90)
        self.timeout.setToolTip(tr("Cancel the query after this many seconds (0 = no limit)"))
        bar.addWidget(self.timeout)
        bar.addStretch(1)
        col.addLayout(bar)
        self.stats = QLabel("")
        self.stats.setTextFormat(Qt.TextFormat.PlainText)
        self.stats.setObjectName("Muted")
        split.addWidget(editor_box)

        self.results = QTabWidget()
        result_page = QWidget()
        rcol = QVBoxLayout(result_page)
        rcol.setContentsMargins(0, 4, 0, 0)
        rcol.setSpacing(4)
        self.result_message = QLabel("")
        self.result_message.setWordWrap(True)
        self.result_message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result_message.setMinimumHeight(self.result_message.fontMetrics().height() + 14)
        self.result_message.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.result_message.hide()
        self.result_model = ResultModel()
        self.result_proxy = ResultSort()
        self.result_proxy.setSourceModel(self.result_model)
        self.grid = QTableView()
        self.grid.setModel(self.result_proxy)
        self.grid.setSortingEnabled(True)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.verticalHeader().setDefaultSectionSize(24)
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.grid.horizontalHeader().setDefaultSectionSize(140)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_menu)
        QShortcut(QKeySequence.StandardKey.Copy, self.grid, activated=self.copy_selection)
        exports = QHBoxLayout()
        exports.addWidget(self.stats)
        exports.addStretch(1)
        self.export_csv = QPushButton(tr("Export CSV"))
        self.export_csv.clicked.connect(lambda: self.export("csv"))
        self.export_json = QPushButton(tr("Export JSON"))
        self.export_json.clicked.connect(lambda: self.export("json"))
        exports.addWidget(self.export_csv)
        exports.addWidget(self.export_json)
        rcol.addWidget(self.result_message)
        rcol.addWidget(self.grid, 1)
        rcol.addLayout(exports)
        self.results.addTab(result_page, tr("Result"))

        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels([tr("When"), tr("Database"), tr("Duration"), tr("Status"), "SQL"])
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.verticalHeader().hide()
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.itemDoubleClicked.connect(self._history_open)
        self.results.addTab(self.history_table, tr("History"))
        split.addWidget(self.results)
        split.setSizes([260, 340])
        self._set_export_enabled(False)
        return split

    def _set_db_enabled(self, on: bool) -> None:
        for w in (self.db_combo, self.table_filter, self.table_list, self.editor, self.run_button,
                  self.disconnect_button, self.max_rows, self.timeout):
            w.setEnabled(on)

    def _set_export_enabled(self, on: bool) -> None:
        self.export_csv.setEnabled(on)
        self.export_json.setEnabled(on)

    # -- DB calls: one at a time, off the GUI thread -------------------------------- #
    def db(self, fn, done=None, failed=None, stale=None) -> None:  # noqa: ANN001
        """Run ``fn`` on the DB thread. ``stale(value)`` cleans up a result nobody wants any more."""
        token = self._token

        def run() -> None:
            try:
                value = fn()
            except Exception as exc:  # noqa: BLE001 - delivered to the GUI
                self._bridge.done.emit(failed or self.db_failed, exc, token)
                return
            if stale is not None and (token != self._token or self._closed):
                stale(value)  # disconnected / closed meanwhile: close what was just opened
                return
            self._bridge.done.emit(done, value, token)

        if self._closed:
            return
        try:
            self._executor.submit(run)
        except RuntimeError:  # executor already shut down (panel closing)
            pass

    def _deliver(self, callback, value, token: int) -> None:  # noqa: ANN001
        if callback is not None and self._alive() and token == self._token:
            callback(value)

    def db_failed(self, exc: BaseException) -> None:
        self.show_toast(str(exc), "error", 9000)

    # -- connection to the database ------------------------------------------------- #
    def on_connected(self) -> None:
        self.show_message(tr("<b>Connected to {host}.</b><br>Choose the database to open through the SSH tunnel.",
                             host=_html(self.spec.title)), [(tr("Connect to a database…"), self.ask_connection)])
        if not self._asked:
            self._asked = True
            self.ask_connection()

    def _password_for(self, profile: SqlProfile) -> str | None:
        if self.credentials is None or not getattr(self.credentials, "available", False):
            return None
        return self.credentials._get(profile.key(self.spec.alias))  # noqa: SLF001 - same store as SSH passwords

    def ask_connection(self) -> None:
        runner = self.runner
        if runner is None or self.connection_lost():
            return

        def detect() -> list[DatabaseCandidate]:  # databases published by Docker containers (best effort)
            try:
                return database_candidates(DockerClient(runner).containers())
            except Exception:  # noqa: BLE001 - no Docker / no permission: nothing to suggest
                return []

        self.other_button.setEnabled(False)
        self.task(detect, self._open_dialog, lambda _e: self._open_dialog([]), name="sql-detect")

    def _open_dialog(self, candidates: list[DatabaseCandidate]) -> None:
        self.other_button.setEnabled(True)
        dlg = SqlConnectDialog(self.spec.title, self.profiles.profiles(self.spec.alias), candidates,
                               self._password_for, self)
        if dlg.exec():
            self.connect_db(dlg.profile(), dlg.password.text())

    def connect_db(self, profile: SqlProfile, password: str) -> None:
        if self._closed:
            return
        self.disconnect_db(quiet=True)
        self.show_message(tr("Opening the SSH tunnel to {where}…", where=_html(f"{profile.host}:{profile.port}")))
        connection = self.connection

        def open_db() -> tuple[SqlTunnel, SqlSession]:
            tunnel = SqlTunnel(connection, profile.host, profile.port)
            try:
                return tunnel, open_session(profile, password, tunnel.local_port)
            except Exception:
                tunnel.close()
                raise

        def opened(value: tuple[SqlTunnel, SqlSession]) -> None:
            self.tunnel, self.session = value
            self.profile = profile
            if profile.remember:
                self.profiles.remember(self.spec.alias, profile)
                if password and self.credentials is not None and getattr(self.credentials, "available", False):
                    self.credentials._set(profile.key(self.spec.alias), password)  # noqa: SLF001
            self.info.setText(tr("{version} · {user}@{host}:{port} · via SSH tunnel", version=self.session.version,
                                 user=profile.user, host=profile.host, port=profile.port)
                              + (" · " + tr("read-only") if profile.read_only else ""))
            self._set_db_enabled(True)
            self.show_content()
            self.load_schema()
            self.load_history()
            self.editor.setFocus()

        def failed(exc: BaseException) -> None:
            self.show_message(tr("<b>Could not connect to the database.</b><br>{error}", error=_html(exc)),
                              [(tr("Try again…"), self.ask_connection)])

        def drop(value: tuple[SqlTunnel, SqlSession]) -> None:
            value[1].close()
            value[0].close()

        self.db(open_db, opened, failed, stale=drop)

    def disconnect_db(self, quiet: bool = False) -> None:
        self._token += 1  # ignore late answers from the old connection
        session, tunnel = self.session, self.tunnel
        self.session = self.tunnel = None
        if session is not None and self._running:  # a query is running: cancel it, off the GUI thread
            threading.Thread(target=session.cancel, name="sql-cancel", daemon=True).start()

        def close_all() -> None:
            if session is not None:
                session.close()
            if tunnel is not None:
                tunnel.close()

        if session is not None or tunnel is not None:
            try:
                self._executor.submit(close_all)
            except RuntimeError:
                threading.Thread(target=close_all, daemon=True).start()
        if hasattr(self, "table_list"):
            self.table_list.clear()
            self.db_combo.clear()
            self.info.setText(tr("Not connected"))
            self._set_db_enabled(False)
            self._running_ui(False)
        if not quiet and self.runner is not None:
            self.show_message(tr("Disconnected from the database."),
                              [(tr("Connect to a database…"), self.ask_connection)])

    def on_closing(self) -> None:
        self.disconnect_db(quiet=True)
        self._executor.shutdown(wait=False)

    # -- schema ------------------------------------------------------------------------ #
    def load_schema(self) -> None:
        session = self.session
        if session is None:
            return

        def read() -> tuple[list[str], list[str], list[str]]:
            names: list[str] = []
            tables = session.tables()
            for table in tables[:200]:  # completion: columns of the first tables (keeps big schemas fast)
                try:
                    names.extend(c.name for c in session.columns(table))
                except SqlError:
                    break
            return session.databases(), tables, names

        def loaded(value: tuple[list[str], list[str], list[str]]) -> None:
            databases, tables, columns = value
            self.db_combo.clear()
            self.db_combo.addItems(databases)
            self.db_combo.setCurrentText(session.database)
            self.table_list.clear()
            for t in tables:
                self.table_list.addItem(QListWidgetItem(t))
            self._filter_tables(self.table_filter.text())
            self.editor.set_names(tables + [t.split(".")[-1] for t in tables] + columns)

        self.db(read, loaded)

    def _filter_tables(self, text: str) -> None:
        text = text.strip().lower()
        for i in range(self.table_list.count()):
            item = self.table_list.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def _database_chosen(self) -> None:
        session, name = self.session, self.db_combo.currentText()
        if session is None or not name or name == session.database:
            return
        self.db(lambda: session.use(name), lambda _r: (self.load_schema(), self.show_toast(
            tr("Using database {name}.", name=name), "ok")))

    def preview_table(self, table: str) -> None:
        if self.session is None:
            return
        self.editor.setPlainText(f"SELECT * FROM {self.session.qualified(table)} LIMIT 100;")
        self.run()

    def _table_menu(self, pos) -> None:  # noqa: ANN001
        item = self.table_list.itemAt(pos)
        if item is None or self.session is None:
            return
        table = item.text()
        menu = QMenu(self)
        menu.addAction(tr("Show structure"), lambda: self.show_structure(table))
        menu.addAction(tr("Count rows"), lambda: self.count_rows(table))
        menu.addAction(tr("Copy name"), lambda: QGuiApplication.clipboard().setText(table))
        menu.exec(self.table_list.viewport().mapToGlobal(pos))

    def show_structure(self, table: str) -> None:
        session = self.session
        if session is None:
            return

        def read() -> tuple[list, list]:
            return session.columns(table), session.indexes(table)

        def shown(value: tuple[list, list]) -> None:
            columns, indexes = value
            rows = [(c.name, c.type, tr("yes") if c.nullable else tr("no"), c.key, c.default) for c in columns]
            rows += [("", "", "", "", "")] if indexes else []
            rows += [(tr("index: {name}", name=n), cols, tr("unique") if unique else "", "", None)
                     for n, cols, unique in indexes]
            self._show_rows([tr("Column"), tr("Type"), tr("Null"), tr("Key"), tr("Default")], rows)
            self.stats.setText(tr("Structure of {table}", table=table))

        self.db(read, shown)

    def count_rows(self, table: str) -> None:
        session = self.session
        if session is not None:
            self.db(lambda: session.count(table),
                    lambda n: self.show_toast(tr("{table}: {count} rows", table=table, count=f"{n:,}"), "ok"))

    # -- running --------------------------------------------------------------------------- #
    def statements_to_run(self) -> list[str]:
        dialect = self.session.kind if self.session else "mysql"
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText().replace(" ", "\n")
            return [t.strip() for _s, _e, t in split_statements(text, dialect)]
        current = statement_at(self.editor.toPlainText(), cursor.position(), dialect)
        return [current] if current else []

    def run(self) -> None:
        session = self.session
        if session is None or self._running:
            return
        statements = self.statements_to_run()
        if not statements:
            return
        reasons = [(s, r) for s in statements if (r := destructive_reason(s, session.kind))]
        if reasons:
            detail = "\n\n".join(f"{r}\n{s[:300]}" for s, r in reasons)
            answer = QMessageBox.warning(self, tr("Confirm"), tr("This will change data that cannot be restored:") +
                                         "\n\n" + detail, QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
                                         QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                return
        max_rows, timeout = self.max_rows.value(), self.timeout.value()
        database = session.database
        self._running_ui(True)

        def execute() -> list[tuple[str, QueryResult | BaseException]]:
            session.set_timeout(timeout)
            out: list[tuple[str, QueryResult | BaseException]] = []
            for sql in statements:
                try:
                    out.append((sql, session.execute(sql, max_rows)))
                except SqlError as exc:
                    out.append((sql, exc))
                    break
            return out

        def finished(results: list[tuple[str, QueryResult | BaseException]]) -> None:
            self._running_ui(False)
            entries = []
            for sql, res in results:
                if isinstance(res, BaseException):
                    status = "cancelled" if isinstance(res, QueryCancelled) else "error"
                    entries.append(HistoryEntry(sql, database, now_text(), 0, status))
                else:
                    entries.append(HistoryEntry(sql, database, now_text(), res.elapsed_ms, "ok",
                                                len(res.rows) if res.has_rows else res.affected or 0))
            self.record_history(entries)
            last_sql, last = results[-1]
            if isinstance(last, BaseException):
                self.show_error(last, last_sql)
            else:
                self.show_result(last, len(results))
            if session.database != database:
                self.load_schema()

        def failed(exc: BaseException) -> None:
            self._running_ui(False)
            self.show_error(exc, statements[0])

        self.db(execute, finished, failed)

    def _running_ui(self, running: bool) -> None:
        self._running = running
        if not hasattr(self, "run_button"):
            return
        self.run_button.setEnabled(not running and self.session is not None)
        self.cancel_button.setEnabled(running)
        if running:
            self.stats.setText(tr("Running…"))

    def cancel(self) -> None:
        session = self.session
        if session is not None and self._running:
            self.task(session.cancel, lambda _r: self.show_toast(tr("Cancel requested."), "warn"), name="sql-cancel")

    # -- results ---------------------------------------------------------------------------- #
    def show_result(self, result: QueryResult, statements: int = 1) -> None:
        self.results.setCurrentIndex(0)
        if result.has_rows:
            self._show_rows(result.columns, result.rows)
            note = tr("Rows: {rows} · {ms} ms", rows=f"{len(result.rows):,}", ms=result.elapsed_ms)
            if result.truncated:
                note += " · " + tr("limited to {max}", max=f"{self.max_rows.value():,}")
            self.stats.setText(note)
            self.result_message.setVisible(result.truncated)
            if result.truncated:
                self._message(tr("Showing the first {max} rows. Raise “Max rows” to see more.",
                                 max=f"{len(result.rows):,}"), "warn")
        else:
            self._show_rows([], [])
            text = tr("{n} rows affected.", n=result.affected or 0) if result.affected is not None else tr("Done.")
            if statements > 1:
                text = tr("{count} statements run. Last: {text}", count=statements, text=text)
            self._message(text, "ok")
            self.stats.setText(tr("{ms} ms", ms=result.elapsed_ms))

    def show_error(self, exc: BaseException, sql: str) -> None:
        self.results.setCurrentIndex(0)
        self._show_rows([], [])
        position = getattr(exc, "position", None)
        where = tr(" (at character {pos})", pos=position) if position else ""
        title = tr("Query cancelled.") if isinstance(exc, QueryCancelled) else tr("Error")
        self._message(f"<b>{title}</b>{where}<br>{_html(exc)}", "error")
        self.stats.setText("")
        if position:  # put the cursor where the database says the problem is
            text = self.editor.toPlainText()
            start = text.find(sql)
            if start >= 0:
                cursor = self.editor.textCursor()
                cursor.setPosition(min(len(text), start + position - 1))
                self.editor.setTextCursor(cursor)

    def _message(self, html: str, level: str) -> None:
        pal = current_palette()
        color = {"ok": pal.success, "error": pal.danger, "warn": pal.warning}.get(level, pal.text)
        self.result_message.setStyleSheet(f"color: {pal.text}; border-left: 3px solid {color}; padding: 6px 10px;"
                                          f" background: {pal.surface_alt};")
        self.result_message.setText(html)
        self.result_message.show()

    def _show_rows(self, columns: list[str], rows: list[tuple]) -> None:
        self.result_message.hide()
        self.result_proxy.sort(-1)
        self.result_model.set_result(columns, rows)
        self._set_export_enabled(bool(columns))
        if columns and len(rows) < 2000:
            self.grid.resizeColumnsToContents()
            for col in range(len(columns)):
                self.grid.setColumnWidth(col, min(self.grid.columnWidth(col), 360))

    def _grid_menu(self, pos) -> None:  # noqa: ANN001
        index = self.grid.indexAt(pos)
        if not index.isValid():
            return
        menu = QMenu(self)
        menu.addAction(tr("Copy cell"), lambda: QGuiApplication.clipboard().setText(
            cell_text(self.result_model.rows[self.result_proxy.mapToSource(index).row()][index.column()])))
        menu.addAction(tr("Copy row"), lambda: self.copy_rows([self.result_proxy.mapToSource(index).row()]))
        menu.addAction(tr("Copy selection"), self.copy_selection)
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def copy_rows(self, rows: list[int]) -> None:
        lines = ["\t".join(cell_text(v) for v in self.result_model.rows[r]) for r in rows]
        QGuiApplication.clipboard().setText("\n".join(lines))

    def copy_selection(self) -> None:
        indexes = sorted(self.grid.selectedIndexes(), key=lambda i: (i.row(), i.column()))
        if not indexes:
            return
        lines: dict[int, list[str]] = {}
        for index in indexes:
            src = self.result_proxy.mapToSource(index)
            lines.setdefault(index.row(), []).append(cell_text(self.result_model.rows[src.row()][src.column()]))
        QGuiApplication.clipboard().setText("\n".join("\t".join(v) for _r, v in sorted(lines.items())))

    def export(self, fmt: str) -> None:
        model = self.result_model
        if not model.columns:
            return
        path, _filter = QFileDialog.getSaveFileName(self, tr("Export"), f"result.{fmt}",
                                                    "CSV (*.csv)" if fmt == "csv" else "JSON (*.json)")
        if not path:
            return
        order = [self.result_proxy.mapToSource(self.result_proxy.index(r, 0)).row() for r in range(model.rowCount())]
        rows = [model.rows[i] for i in order]
        try:
            Path(path).write_text(export_text(fmt, model.columns, rows), encoding="utf-8")
        except OSError as exc:
            self.show_toast(str(exc), "error")
            return
        self.show_toast(tr("Saved {rows} rows to {path}", rows=len(rows), path=path), "ok")

    # -- history ---------------------------------------------------------------------- #
    def load_history(self) -> None:
        alias = self.spec.alias
        self.db(lambda: self.history.entries(alias), self._fill_history)

    def record_history(self, entries: list[HistoryEntry]) -> None:
        """Save (passwords redacted) and refresh the History tab, off the GUI thread."""
        alias = self.spec.alias

        def save() -> list[HistoryEntry]:
            for entry in entries:
                self.history.add(alias, entry)
            return self.history.entries(alias)

        self.db(save, self._fill_history)

    def _fill_history(self, entries: list[HistoryEntry]) -> None:
        self.history_table.setRowCount(len(entries))
        status_text = {"ok": tr("ok"), "error": tr("error"), "cancelled": tr("cancelled")}
        for row, e in enumerate(entries):
            values = [e.when, e.database, f"{e.ms} ms", status_text.get(e.status, e.status),
                      " ".join(e.sql.split())[:300]]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(ROLE_ROW, e.sql)
                self.history_table.setItem(row, col, item)

    def _history_open(self, item: QTableWidgetItem) -> None:
        self.editor.setPlainText(item.data(ROLE_ROW))
        self.editor.setFocus()

    def focus_target(self) -> QWidget:
        return self.editor if self.stack.currentWidget() is self.content else super().focus_target()


def export_text(fmt: str, columns: list[str], rows: list[tuple]) -> str:
    if fmt == "json":
        return json.dumps([{c: json_value(v) for c, v in zip(columns, row, strict=False)} for row in rows],
                          ensure_ascii=False, indent=2)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if v is None else csv_cell(v) for v in row])
    return buf.getvalue()


def csv_cell(value: Any) -> str:  # noqa: ANN401
    """A cell for CSV; text that a spreadsheet would run as a formula is prefixed with '."""
    text = cell_text(value)
    if isinstance(value, str) and text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _html(value: object) -> str:
    from html import escape

    text = str(value) or (type(value).__name__ if isinstance(value, BaseException) else "")
    return escape(text)
