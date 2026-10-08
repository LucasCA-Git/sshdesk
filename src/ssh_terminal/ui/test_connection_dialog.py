"""'Test Connection' dialog: DNS -> TCP -> SSH authentication, run in background."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ssh_terminal.errors import FriendlyError
from ssh_terminal.ssh.resolver import ResolvedConfig
from ssh_terminal.ssh.ssh_auth import AuthPrompter
from ssh_terminal.ssh.ssh_manager import SSHManager, StepStatus, TestReport, TestStep
from ssh_terminal.ui.dialogs import ErrorDialog, mark_primary
from ssh_terminal.ui.theme import current_palette
from ssh_terminal.ui.workers import run_async

STEP_NAMES = {"DNS": "Host lookup", "TCP": "TCP port", "SSH": "SSH authentication"}


class TestConnectionDialog(QDialog):
    def __init__(
        self,
        ssh: SSHManager,
        resolved: ResolvedConfig,
        prompter: AuthPrompter,
        password: str | None = None,
        parent: QWidget | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Test Connection — {resolved.alias}")
        self.setMinimumWidth(480)
        self._details = ""
        layout = QVBoxLayout(self)
        header = QLabel(f"Testing <b>{resolved.user}@{resolved.hostname}:{resolved.port}</b>")
        layout.addWidget(header)
        if resolved.proxy_jump:
            via = QLabel(f"via ProxyJump {resolved.proxy_jump}")
            via.setProperty("muted", True)
            layout.addWidget(via)
        if hint:
            note = QLabel(hint)
            note.setWordWrap(True)
            note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(note)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self._rows: dict[str, tuple[QLabel, QLabel]] = {}
        for row, key in enumerate(("DNS", "TCP", "SSH")):
            mark = QLabel("•")
            mark.setFixedWidth(18)
            mark.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
            text = QLabel(f"{STEP_NAMES[key]}: waiting…")
            text.setWordWrap(True)
            text.setProperty("muted", True)
            grid.addWidget(mark, row, 0)
            grid.addWidget(text, row, 1)
            self._rows[key] = (mark, text)
        layout.addLayout(grid)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        buttons = QHBoxLayout()
        self.details_button = QPushButton("View technical details")
        self.details_button.setVisible(False)
        self.details_button.clicked.connect(self._show_details)
        buttons.addWidget(self.details_button)
        buttons.addStretch(1)
        self.close_button = mark_primary(QPushButton("Close"))
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self._start(ssh, resolved, prompter, password)

    def _start(self, ssh: SSHManager, resolved: ResolvedConfig, prompter: AuthPrompter, password: str | None) -> None:
        def job(progress):  # type: ignore[no-untyped-def]
            return ssh.test_connection(resolved, prompter, on_step=progress, password=password)

        self._bridge = run_async(job, on_progress=self._on_step, on_done=self._on_done, pass_progress=True, name="test-connection")

    def _on_step(self, step: TestStep) -> None:
        pal = current_palette()
        mark, text = self._rows[step.name]
        symbol, color = {
            StepStatus.RUNNING: ("…", pal.warning),
            StepStatus.OK: ("✓", pal.success),
            StepStatus.FAILED: ("✗", pal.danger),
            StepStatus.SKIPPED: ("–", pal.muted),
        }[step.status]
        mark.setText(symbol)
        mark.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 15px;")
        text.setText(step.message)
        text.setProperty("muted", step.status in (StepStatus.RUNNING, StepStatus.SKIPPED))
        text.style().unpolish(text)
        text.style().polish(text)
        if step.status is StepStatus.FAILED:
            self._details = step.details

    def _on_done(self, report: TestReport) -> None:
        pal = current_palette()
        if report.success:
            self.summary.setText(f"<span style='color:{pal.success}'><b>Connection successful</b></span> ({report.duration:.1f}s)")
        else:
            self.summary.setText(f"<span style='color:{pal.danger}'><b>✗ Connection failed</b></span>")
            self.details_button.setVisible(bool(self._details))

    def _show_details(self) -> None:
        ErrorDialog(FriendlyError("Technical details", "Details of the failed step:", [], self._details), self).exec()
