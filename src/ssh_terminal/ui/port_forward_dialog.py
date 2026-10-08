"""Port forwarding manager for an active SSH session."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.models.connection import PortForwardSpec
from ssh_terminal.ssh.forwarding import ForwardingManager
from ssh_terminal.ui.dialogs import mark_primary
from ssh_terminal.ui.workers import run_async


class PortForwardDialog(QDialog):
    def __init__(self, title: str, manager: ForwardingManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle(f"Port Forwarding — {title}")
        self.resize(640, 460)
        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Type", "Forward", "Connections", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        stop = QPushButton("Stop Selected")
        stop.clicked.connect(self._stop)
        layout.addWidget(stop)

        box = QGroupBox("New forward")
        form = QFormLayout(box)
        self.kind = QComboBox()
        self.kind.addItem("Local (-L): local port → remote target", "local")
        self.kind.addItem("Remote (-R): server port → local target", "remote")
        self.kind.addItem("Dynamic (-D): local SOCKS proxy", "dynamic")
        self.kind.currentIndexChanged.connect(self._kind_changed)
        form.addRow("Type", self.kind)
        bind_row = QHBoxLayout()
        self.bind_addr = QLineEdit("127.0.0.1")
        self.bind_port = QLineEdit()
        self.bind_port.setValidator(QIntValidator(0, 65535))
        self.bind_port.setPlaceholderText("8080")
        bind_row.addWidget(self.bind_addr, 2)
        bind_row.addWidget(QLabel(":"))
        bind_row.addWidget(self.bind_port, 1)
        form.addRow("Listen on", bind_row)
        target_row = QHBoxLayout()
        self.target_host = QLineEdit("localhost")
        self.target_port = QLineEdit()
        self.target_port.setValidator(QIntValidator(1, 65535))
        self.target_port.setPlaceholderText("80")
        target_row.addWidget(self.target_host, 2)
        target_row.addWidget(QLabel(":"))
        target_row.addWidget(self.target_port, 1)
        self.target_label = QLabel("Target")
        form.addRow(self.target_label, target_row)
        self._target_widgets = (self.target_host, self.target_port)
        add = mark_primary(QPushButton("Start Forward"))
        add.clicked.connect(self._add)
        form.addRow("", add)
        layout.addWidget(box)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)
        self._refresh()

    def _kind_changed(self) -> None:
        dynamic = self.kind.currentData() == "dynamic"
        for w in self._target_widgets:
            w.setEnabled(not dynamic)

    def _refresh(self) -> None:
        forwards = self.manager.list()
        self.table.setRowCount(len(forwards))
        for r, fwd in enumerate(forwards):
            for c, text in enumerate((fwd.spec.kind, fwd.spec.describe(), str(fwd.connections), "active" if fwd.active else "stopped")):
                item = QTableWidgetItem(text)
                item.setData(256, fwd.id)
                self.table.setItem(r, c, item)

    def _add(self) -> None:
        kind = self.kind.currentData()
        try:
            spec = PortForwardSpec(
                kind,
                self.bind_addr.text().strip() or "127.0.0.1",
                int(self.bind_port.text() or "0"),
                self.target_host.text().strip(),
                int(self.target_port.text() or "0"),
            )
            if kind != "dynamic" and (not spec.target_host or not spec.target_port):
                raise ValueError("Target host and port are required.")
        except ValueError as exc:
            QMessageBox.warning(self, "Port forwarding", f"Could not start forward:\n\n{exc}")
            return
        # Remote forwards wait for the server's answer: never on the GUI thread.
        self._task = run_async(
            self.manager.start,
            spec,
            on_done=lambda _f: self._refresh(),
            on_error=lambda exc: QMessageBox.warning(self, "Port forwarding", f"Could not start forward:\n\n{exc}"),
            name="port-forward",
        )

    def _stop(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        for r in rows:
            item = self.table.item(r, 0)
            if item is not None:
                self.manager.stop(int(item.data(256)))
        self._refresh()
