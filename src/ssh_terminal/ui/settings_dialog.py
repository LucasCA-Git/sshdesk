"""Settings dialog: General, Appearance, Terminal, SSH, Keyboard, Security."""

from __future__ import annotations

import copy
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.models.app_settings import DEFAULT_KEYBINDINGS, KEYBINDING_LABELS, AppSettings
from ssh_terminal.terminal.color_schemes import SCHEMES
from ssh_terminal.ui.dialogs import mark_primary
from ssh_terminal.utils.paths import get_ssh_config_path

CATEGORIES = ["General", "Appearance", "Terminal", "SSH", "Keyboard", "Security"]


def _page(title: str) -> tuple[QWidget, QFormLayout]:
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(20, 16, 20, 16)
    header = QLabel(title)
    header.setObjectName("DialogHeader")
    outer.addWidget(header)
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setVerticalSpacing(10)
    form.setHorizontalSpacing(16)
    outer.addLayout(form)
    outer.addStretch(1)
    return page, form


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        shells: list[str],
        clear_credentials: Callable[[], int],
        keyring_backend: str,
        open_diagnostics: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(760, 560)
        self.result_settings = copy.deepcopy(settings)
        self._clear_credentials = clear_credentials
        s = self.result_settings

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(170)
        self.nav.addItems(CATEGORIES)
        self.stack = QStackedWidget()
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        # General -------------------------------------------------------
        page, form = _page("General")
        self.config_path = QLineEdit(s.ssh_config_path)
        self.config_path.setPlaceholderText(str(get_ssh_config_path()))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_config)
        row = QHBoxLayout()
        row.addWidget(self.config_path, 1)
        row.addWidget(browse)
        form.addRow("SSH config file", row)
        self.default_shell = QComboBox()
        self.default_shell.addItem("(platform default)", "")
        for name in shells:
            self.default_shell.addItem(name, name)
        self.default_shell.setCurrentIndex(max(0, self.default_shell.findData(s.default_local_shell)))
        form.addRow("Local terminal", self.default_shell)
        self.watch = QCheckBox("Watch the SSH config for external changes")
        self.watch.setChecked(s.watch_config)
        form.addRow("", self.watch)
        self.auto_reload = QCheckBox("Reload automatically (don't ask)")
        self.auto_reload.setChecked(s.auto_reload_config)
        form.addRow("", self.auto_reload)
        self.confirm_close = QCheckBox("Confirm before closing active connections")
        self.confirm_close.setChecked(s.confirm_close_connected)
        form.addRow("", self.confirm_close)
        self.restore_tabs = QCheckBox("Reopen tabs from the last session on startup")
        self.restore_tabs.setChecked(s.restore_tabs)
        form.addRow("", self.restore_tabs)
        diag = QPushButton("Open Diagnostics...")
        diag.clicked.connect(open_diagnostics)
        form.addRow("Diagnostics", diag)
        self.stack.addWidget(page)

        # Appearance ------------------------------------------------------
        page, form = _page("Appearance")
        self.theme = QComboBox()
        for label, value in (("Dark", "dark"), ("Light", "light"), ("System", "system")):
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))
        form.addRow("Theme", self.theme)
        self.scheme = QComboBox()
        self.scheme.addItems(list(SCHEMES))
        self.scheme.setCurrentText(s.color_scheme)
        form.addRow("Terminal colors", self.scheme)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(50, 100)
        self.opacity.setValue(s.window_opacity)
        self.opacity_label = QLabel(f"{s.window_opacity}%")
        self.opacity.valueChanged.connect(lambda v: self.opacity_label.setText(f"{v}%"))
        row = QHBoxLayout()
        row.addWidget(self.opacity, 1)
        row.addWidget(self.opacity_label)
        form.addRow("Window opacity", row)
        self.stack.addWidget(page)

        # Terminal --------------------------------------------------------
        page, form = _page("Terminal")
        self.font_family = QFontComboBox()
        self.font_family.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self.font_family.setCurrentFont(QFont(s.font_family))
        self.font_family.setEditable(True)
        self.font_family.setCurrentText(s.font_family)
        form.addRow("Font", self.font_family)
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 48)
        self.font_size.setValue(s.font_size)
        form.addRow("Size", self.font_size)
        cursor_row = QHBoxLayout()
        self.cursor_group = QButtonGroup(self)
        for i, (label, value) in enumerate((("Block", "block"), ("Underline", "underline"), ("Bar", "bar"))):
            radio = QRadioButton(label)
            radio.setProperty("value", value)
            radio.setChecked(s.cursor_style == value)
            self.cursor_group.addButton(radio, i)
            cursor_row.addWidget(radio)
        cursor_row.addStretch(1)
        form.addRow("Cursor", cursor_row)
        self.blink = QCheckBox("Cursor blink")
        self.blink.setChecked(s.cursor_blink)
        form.addRow("", self.blink)
        self.scrollback = QSpinBox()
        self.scrollback.setRange(0, 1_000_000)
        self.scrollback.setSingleStep(1000)
        self.scrollback.setValue(s.scrollback_lines)
        self.scrollback.setSuffix(" lines")
        form.addRow("Scrollback", self.scrollback)
        self.copy_on_select = QCheckBox("Copy on select")
        self.copy_on_select.setChecked(s.copy_on_select)
        form.addRow("", self.copy_on_select)
        self.paste_right = QCheckBox("Paste on right click (copy if text is selected)")
        self.paste_right.setChecked(s.paste_on_right_click)
        form.addRow("", self.paste_right)
        self.bell = QComboBox()
        for label, value in (("None", "none"), ("Visual flash", "visual"), ("Sound", "sound")):
            self.bell.addItem(label, value)
        self.bell.setCurrentIndex(max(0, self.bell.findData(s.bell)))
        form.addRow("Bell", self.bell)
        self.term_type = QComboBox()
        self.term_type.setEditable(True)
        self.term_type.addItems(["xterm-256color", "xterm", "vt100", "linux"])
        self.term_type.setCurrentText(s.term_type)
        form.addRow("TERM", self.term_type)
        self.stack.addWidget(page)

        # SSH -----------------------------------------------------------------
        page, form = _page("SSH")
        self.use_openssh = QCheckBox("Resolve effective configuration with OpenSSH (ssh -G) when available")
        self.use_openssh.setChecked(s.use_openssh_resolver)
        form.addRow("", self.use_openssh)
        self.use_agent = QCheckBox("Use SSH agent (ssh-agent / Pageant / Windows OpenSSH agent)")
        self.use_agent.setChecked(s.use_agent)
        form.addRow("", self.use_agent)
        self.config_forwards = QCheckBox("Start LocalForward/RemoteForward/DynamicForward from the SSH config")
        self.config_forwards.setChecked(s.apply_config_forwards)
        form.addRow("", self.config_forwards)
        self.timeout = QSpinBox()
        self.timeout.setRange(3, 300)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(s.connect_timeout)
        form.addRow("Connect timeout", self.timeout)
        self.keepalive = QSpinBox()
        self.keepalive.setRange(0, 3600)
        self.keepalive.setSuffix(" s")
        self.keepalive.setValue(s.keepalive_interval)
        self.keepalive.setToolTip("Used when the host has no ServerAliveInterval. 0 disables keepalives.")
        form.addRow("Keepalive", self.keepalive)
        self.auto_reconnect = QCheckBox("Reconnect automatically when a connection is lost")
        self.auto_reconnect.setChecked(s.auto_reconnect)
        form.addRow("", self.auto_reconnect)
        self.attempts = QSpinBox()
        self.attempts.setRange(1, 50)
        self.attempts.setValue(s.reconnect_attempts)
        form.addRow("Reconnect attempts", self.attempts)
        self.stack.addWidget(page)

        # Keyboard ----------------------------------------------------------
        page, form = _page("Keyboard")
        self.keys_table = QTableWidget(len(DEFAULT_KEYBINDINGS), 2)
        self.keys_table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        self.keys_table.verticalHeader().setVisible(False)
        self.keys_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.keys_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._key_edits: dict[str, QKeySequenceEdit] = {}
        for row_i, action in enumerate(DEFAULT_KEYBINDINGS):
            item = QTableWidgetItem(KEYBINDING_LABELS.get(action, action))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.keys_table.setItem(row_i, 0, item)
            edit = QKeySequenceEdit(QKeySequence(s.keybindings.get(action, "")))
            edit.setClearButtonEnabled(True)
            self.keys_table.setCellWidget(row_i, 1, edit)
            self._key_edits[action] = edit
        self.keys_table.setMinimumHeight(300)
        note = QLabel("Keys not listed here are sent to the terminal. Tip: Ctrl+W is also “delete word” in bash; "
                      "rebind Close tab to Ctrl+Shift+W if you prefer.")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        reset = QPushButton("Restore defaults")
        reset.clicked.connect(self._reset_keys)
        box = QVBoxLayout()
        box.addWidget(self.keys_table)
        box.addWidget(note)
        box.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        form.addRow(box)
        self.stack.addWidget(page)

        # Security --------------------------------------------------------------
        page, form = _page("Security")
        self.allow_keyring = QCheckBox("Allow saving passwords/passphrases (encrypted)")
        self.allow_keyring.setChecked(s.allow_keyring)
        form.addRow("", self.allow_keyring)
        form.addRow("Stored in", QLabel(keyring_backend))
        self.strict = QComboBox()
        for label, value in (
            ("Ask (recommended)", "ask"),
            ("Accept new keys automatically (accept-new)", "accept-new"),
            ("Strict: reject unknown hosts (yes)", "yes"),
        ):
            self.strict.addItem(label, value)
        self.strict.setCurrentIndex(max(0, self.strict.findData(s.default_strict_host_key_checking)))
        form.addRow("Unknown host keys", self.strict)
        self.backups = QSpinBox()
        self.backups.setRange(1, 500)
        self.backups.setValue(s.backup_count)
        form.addRow("SSH config backups kept", self.backups)
        clear = QPushButton("Clear saved credentials")
        clear.clicked.connect(self._clear)
        form.addRow("", clear)
        info = QLabel("Passwords are never written to app_config.json or to the SSH config. Private keys are read "
                      "from their location and never copied. Nothing is sent to external services.")
        info.setWordWrap(True)
        info.setProperty("muted", True)
        form.addRow(info)
        self.stack.addWidget(page)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(16, 10, 16, 14)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = mark_primary(QPushButton("Save"))
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        root.addLayout(buttons)

    def select_category(self, name: str) -> None:
        if name in CATEGORIES:
            self.nav.setCurrentRow(CATEGORIES.index(name))

    def _browse_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "SSH config file", str(get_ssh_config_path().parent))
        if path:
            self.config_path.setText(path)

    def _reset_keys(self) -> None:
        for action, edit in self._key_edits.items():
            edit.setKeySequence(QKeySequence(DEFAULT_KEYBINDINGS[action]))

    def _clear(self) -> None:
        count = self._clear_credentials()
        QMessageBox.information(self, "Credentials", f"{count} saved credential(s) removed.")

    def _accept(self) -> None:
        s = self.result_settings
        s.ssh_config_path = self.config_path.text().strip()
        s.default_local_shell = self.default_shell.currentData() or ""
        s.watch_config = self.watch.isChecked()
        s.auto_reload_config = self.auto_reload.isChecked()
        s.confirm_close_connected = self.confirm_close.isChecked()
        s.restore_tabs = self.restore_tabs.isChecked()
        s.theme = self.theme.currentData()
        s.color_scheme = self.scheme.currentText()
        s.window_opacity = self.opacity.value()
        s.font_family = self.font_family.currentText().strip() or s.font_family
        s.font_size = self.font_size.value()
        checked = self.cursor_group.checkedButton()
        s.cursor_style = checked.property("value") if checked else "block"
        s.cursor_blink = self.blink.isChecked()
        s.scrollback_lines = self.scrollback.value()
        s.copy_on_select = self.copy_on_select.isChecked()
        s.paste_on_right_click = self.paste_right.isChecked()
        s.bell = self.bell.currentData()
        s.term_type = self.term_type.currentText().strip() or "xterm-256color"
        s.use_openssh_resolver = self.use_openssh.isChecked()
        s.use_agent = self.use_agent.isChecked()
        s.apply_config_forwards = self.config_forwards.isChecked()
        s.connect_timeout = self.timeout.value()
        s.keepalive_interval = self.keepalive.value()
        s.auto_reconnect = self.auto_reconnect.isChecked()
        s.reconnect_attempts = self.attempts.value()
        s.allow_keyring = self.allow_keyring.isChecked()
        s.default_strict_host_key_checking = self.strict.currentData()
        s.backup_count = self.backups.value()
        bindings = {}
        seen: dict[str, str] = {}
        for action, edit in self._key_edits.items():
            text = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            if text and text in seen:
                QMessageBox.warning(self, "Shortcut conflict",
                                    f"“{text}” is assigned to both “{KEYBINDING_LABELS[seen[text]]}” and “{KEYBINDING_LABELS[action]}”.")
                self.select_category("Keyboard")
                return
            if text:
                seen[text] = action
            bindings[action] = text
        s.keybindings = bindings
        self.accept()

