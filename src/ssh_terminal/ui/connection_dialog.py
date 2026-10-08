"""New / Edit SSH connection dialog."""

from __future__ import annotations

import copy
from collections.abc import Callable

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QIntValidator, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.models.connection import PortForwardSpec
from ssh_terminal.models.ssh_host import KNOWN_KEYWORDS, SSHHost, is_pattern
from ssh_terminal.ssh.config_parser import split_arguments
from ssh_terminal.ui.dialogs import mark_primary
from ssh_terminal.utils.paths import contract_user_path, expand_user_path, get_ssh_dir

AUTH_KEY = "key"
AUTH_PASSWORD = "password"
AUTH_AGENT = "agent"
PASSWORD_PREFS = "password,keyboard-interactive"

TRI_STATE_TIP = "Partially checked = not set (OpenSSH default / inherited value)"


def _tri_state(value: bool | None) -> Qt.CheckState:
    if value is None:
        return Qt.CheckState.PartiallyChecked
    return Qt.CheckState.Checked if value else Qt.CheckState.Unchecked


def _from_tri_state(state: Qt.CheckState) -> bool | None:
    if state == Qt.CheckState.PartiallyChecked:
        return None
    return state == Qt.CheckState.Checked


def _int_or_none(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def find_private_keys() -> list[str]:
    """Private keys in ~/.ssh (files with a matching .pub)."""
    ssh_dir = get_ssh_dir()
    keys = []
    if ssh_dir.is_dir():
        for pub in sorted(ssh_dir.glob("*.pub")):
            private = pub.with_suffix("")
            if private.is_file():
                keys.append(contract_user_path(private))
    return keys


class ConnectionDialog(QDialog):
    """Edits an :class:`SSHHost`. Result is available in :attr:`host` after ``accept``.

    ``password`` / ``save_password`` carry a password typed for password
    authentication; it is never written to the SSH config.
    """

    def __init__(
        self,
        host: SSHHost | None,
        existing_aliases: set[str],
        groups: list[str],
        group: str | None = None,
        on_test: Callable[[SSHHost, str | None], None] | None = None,
        keyring_available: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.original = copy.deepcopy(host) if host else None
        self.host = copy.deepcopy(host) if host else SSHHost(alias="")
        self.is_defaults = bool(host and host.is_wildcard)
        self.existing = existing_aliases - set(self.original.patterns if self.original else [])
        self.on_test = on_test
        self.password: str | None = None
        self.save_password = False
        self.group: str | None = group

        if self.is_defaults:
            self.setWindowTitle(f"SSH Defaults — Host {' '.join(self.host.patterns)}")
        else:
            self.setWindowTitle("Edit SSH Connection" if host else "New SSH Connection")
        self.setMinimumWidth(600)

        layout = QVBoxLayout(self)
        header = QLabel(self.windowTitle())
        header.setObjectName("DialogHeader")
        layout.addWidget(header)
        if self.is_defaults:
            note = QLabel("These options apply to every host matching the pattern(s). "
                          "OpenSSH uses the first value found, so specific hosts override them only if they appear earlier in the file.")
            note.setWordWrap(True)
            note.setProperty("muted", True)
            layout.addWidget(note)
        if self.original and self.original.source_file:
            src = QLabel(f"Stored in {self.original.source_file}")
            src.setProperty("muted", True)
            layout.addWidget(src)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("DialogTabs")
        self.tabs.addTab(self._general_tab(groups, keyring_available), "General")
        self.tabs.addTab(self._advanced_tab(), "Advanced")
        self.tabs.addTab(self._forward_tab(), "Port Forwarding")
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self._test)
        self.test_button.setVisible(on_test is not None and not self.is_defaults)
        buttons.addWidget(self.test_button)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = mark_primary(QPushButton("Save"))
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self._load()

    # ------------------------------------------------------------------ #
    def _general_tab(self, groups: list[str], keyring_available: bool) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)

        self.alias_edit = QLineEdit()
        self.alias_edit.setPlaceholderText("production-server")
        if not self.is_defaults:
            self.alias_edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[^\s\"#]+")))
        form.addRow("Name", self.alias_edit)
        self.aliases_edit = QLineEdit()
        self.aliases_edit.setPlaceholderText("optional, space separated (e.g. prod p1)")
        form.addRow("Aliases", self.aliases_edit)
        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText("10.0.0.10 or server.example.com")
        form.addRow("Host", self.hostname_edit)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("default: inherited / local user")
        form.addRow("Username", self.user_edit)
        self.port_edit = QLineEdit()
        self.port_edit.setPlaceholderText("22")
        self.port_edit.setValidator(QIntValidator(1, 65535))
        form.addRow("Port", self.port_edit)

        self.group_combo = QComboBox()
        self.group_combo.setEditable(True)
        self.group_combo.addItem("")
        self.group_combo.addItems(groups)
        self.group_combo.lineEdit().setPlaceholderText("No group (visual only, not saved in SSH config)")
        form.addRow("Group", self.group_combo)

        auth_row = QHBoxLayout()
        auth_row.setSpacing(18)
        self.auth_group = QButtonGroup(self)
        self.auth_key = QRadioButton("SSH Key")
        self.auth_password = QRadioButton("Password")
        self.auth_agent = QRadioButton("Agent / Automatic")
        for i, radio in enumerate((self.auth_key, self.auth_password, self.auth_agent)):
            self.auth_group.addButton(radio, i)
            auth_row.addWidget(radio)
        auth_row.addStretch(1)
        self.auth_group.idToggled.connect(self._auth_changed)
        form.addRow("Authentication", auth_row)

        self.key_widget = QWidget()
        key_layout = QVBoxLayout(self.key_widget)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self.key_list = QListWidget()
        self.key_list.setMaximumHeight(84)
        key_layout.addWidget(self.key_list)
        key_buttons = QHBoxLayout()
        self.key_combo = QComboBox()
        self.key_combo.setEditable(True)
        self.key_combo.addItems(find_private_keys())
        self.key_combo.setCurrentText("")
        self.key_combo.lineEdit().setPlaceholderText("~/.ssh/id_ed25519")
        key_buttons.addWidget(self.key_combo, 1)
        add_key = QPushButton("Add")
        add_key.clicked.connect(self._add_key_from_combo)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_key)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: self.key_list.takeItem(self.key_list.currentRow()))
        key_buttons.addWidget(add_key)
        key_buttons.addWidget(browse)
        key_buttons.addWidget(remove)
        key_layout.addLayout(key_buttons)
        form.addRow("Identity Files", self.key_widget)

        self.password_widget = QWidget()
        pw_layout = QVBoxLayout(self.password_widget)
        pw_layout.setContentsMargins(0, 0, 0, 0)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("leave empty to be asked when connecting")
        pw_layout.addWidget(self.password_edit)
        self.save_password_check = QCheckBox("Save password (don't ask when connecting)")
        self.save_password_check.setEnabled(keyring_available)
        self.save_password_check.setToolTip(
            "Stored encrypted (system keyring or encrypted local vault), never in the SSH config"
            if keyring_available else "Saving secrets is disabled in Settings")
        pw_layout.addWidget(self.save_password_check)
        form.addRow("Password", self.password_widget)

        self.agent_label = QLabel("Uses keys loaded in ssh-agent / Pageant and the default keys in ~/.ssh.")
        self.agent_label.setWordWrap(True)
        self.agent_label.setProperty("muted", True)
        form.addRow("", self.agent_label)

        self.jump_combo = QComboBox()
        self.jump_combo.setEditable(True)
        self.jump_combo.addItem("")
        self.jump_combo.addItems(sorted(a for a in self.existing if not is_pattern(a)))
        self.jump_combo.lineEdit().setPlaceholderText("bastion  or  user@jump:22,second-hop")
        form.addRow("ProxyJump", self.jump_combo)

        if self.is_defaults:
            self.alias_edit.setReadOnly(False)
            self.hostname_edit.setPlaceholderText("usually empty for defaults")
            for w in (self.group_combo,):
                w.setEnabled(False)
        return page

    def _advanced_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.forward_agent = QCheckBox("ForwardAgent")
        self.forward_x11 = QCheckBox("ForwardX11")
        self.compression = QCheckBox("Compression")
        self.identities_only = QCheckBox("IdentitiesOnly")
        for box in (self.forward_agent, self.forward_x11, self.compression, self.identities_only):
            box.setTristate(True)
            box.setToolTip(TRI_STATE_TIP)
        flags = QGridLayout()
        for i, box in enumerate((self.forward_agent, self.forward_x11, self.compression, self.identities_only)):
            flags.addWidget(box, i // 2, i % 2)
        form.addRow("Options", flags)
        self.alive_interval = QLineEdit()
        self.alive_interval.setValidator(QIntValidator(0, 86400))
        self.alive_interval.setPlaceholderText("seconds (e.g. 60)")
        form.addRow("ServerAliveInterval", self.alive_interval)
        self.alive_count = QLineEdit()
        self.alive_count.setValidator(QIntValidator(0, 1000))
        self.alive_count.setPlaceholderText("3")
        form.addRow("ServerAliveCountMax", self.alive_count)
        self.strict_combo = QComboBox()
        self.strict_combo.addItem("(not set)", "")
        for value in ("ask", "accept-new", "yes", "no"):
            self.strict_combo.addItem(value, value)
        form.addRow("StrictHostKeyChecking", self.strict_combo)
        self.add_keys_combo = QComboBox()
        self.add_keys_combo.addItem("(not set)", "")
        for value in ("yes", "no", "ask", "confirm"):
            self.add_keys_combo.addItem(value, value)
        form.addRow("AddKeysToAgent", self.add_keys_combo)
        self.prefs_edit = QLineEdit()
        self.prefs_edit.setPlaceholderText("publickey,keyboard-interactive,password")
        form.addRow("PreferredAuthentications", self.prefs_edit)
        self.proxy_command_edit = QLineEdit()
        self.proxy_command_edit.setPlaceholderText("e.g. ssh -W %h:%p bastion")
        form.addRow("ProxyCommand", self.proxy_command_edit)

        self.extra_table = QTableWidget(0, 2)
        self.extra_table.setHorizontalHeaderLabels(["Option", "Value"])
        self.extra_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.extra_table.horizontalHeader().setStretchLastSection(True)
        self.extra_table.verticalHeader().setVisible(False)
        self.extra_table.setMinimumHeight(110)
        extra_box = QVBoxLayout()
        extra_box.addWidget(self.extra_table)
        row = QHBoxLayout()
        add = QPushButton("Add Option")
        add.clicked.connect(lambda: self._add_row(self.extra_table, "", ""))
        rem = QPushButton("Remove")
        rem.clicked.connect(lambda: self.extra_table.removeRow(self.extra_table.currentRow()))
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        extra_box.addLayout(row)
        form.addRow("Other options", extra_box)
        return page

    def _forward_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        info = QLabel("Forwards saved here are written to the SSH config (LocalForward / RemoteForward / "
                      "DynamicForward) and started automatically when you connect.\n"
                      "Format: “8080 localhost:80” (local/remote) or “1080” (dynamic SOCKS).")
        info.setWordWrap(True)
        info.setProperty("muted", True)
        layout.addWidget(info)
        self.forward_table = QTableWidget(0, 2)
        self.forward_table.setHorizontalHeaderLabels(["Type", "Specification"])
        self.forward_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.forward_table.horizontalHeader().setStretchLastSection(True)
        self.forward_table.verticalHeader().setVisible(False)
        layout.addWidget(self.forward_table, 1)
        row = QHBoxLayout()
        for label, kind in (("Add Local", "local"), ("Add Remote", "remote"), ("Add Dynamic", "dynamic")):
            button = QPushButton(label)
            button.clicked.connect(lambda _c=False, k=kind: self._add_forward(k, ""))
            row.addWidget(button)
        rem = QPushButton("Remove")
        rem.clicked.connect(lambda: self.forward_table.removeRow(self.forward_table.currentRow()))
        row.addWidget(rem)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    # ------------------------------------------------------------------ #
    @staticmethod
    def _add_row(table: QTableWidget, key: str, value: str) -> None:
        r = table.rowCount()
        table.insertRow(r)
        table.setItem(r, 0, QTableWidgetItem(key))
        table.setItem(r, 1, QTableWidgetItem(value))
        if not key:
            table.setCurrentCell(r, 0)
            table.editItem(table.item(r, 0))

    def _add_forward(self, kind: str, spec: str) -> None:
        r = self.forward_table.rowCount()
        self.forward_table.insertRow(r)
        combo = QComboBox()
        combo.addItems(["local", "remote", "dynamic"])
        combo.setCurrentText(kind)
        self.forward_table.setCellWidget(r, 0, combo)
        self.forward_table.setItem(r, 1, QTableWidgetItem(spec))
        if not spec:
            self.forward_table.setCurrentCell(r, 1)
            self.forward_table.editItem(self.forward_table.item(r, 1))

    def _add_key(self, path: str) -> None:
        path = path.strip()
        if not path:
            return
        existing = [self.key_list.item(i).text() for i in range(self.key_list.count())]
        if path not in existing:
            self.key_list.addItem(path)

    def _add_key_from_combo(self) -> None:
        self._add_key(self.key_combo.currentText())
        self.key_combo.setCurrentText("")

    def _browse_key(self) -> None:
        start = str(get_ssh_dir()) if get_ssh_dir().is_dir() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Select private key", start, "All files (*)")
        if path:
            self._add_key(contract_user_path(path))

    def _auth_changed(self, _id: int, checked: bool) -> None:
        if not checked:
            return
        mode = self._auth_mode()
        self.key_widget.setVisible(mode == AUTH_KEY)
        self.password_widget.setVisible(mode == AUTH_PASSWORD)
        self.agent_label.setVisible(mode == AUTH_AGENT)
        form_parent = self.key_widget.parentWidget()
        if form_parent is not None:
            layout = form_parent.layout()
            if isinstance(layout, QFormLayout):
                layout.setRowVisible(self.key_widget, mode == AUTH_KEY)
                layout.setRowVisible(self.password_widget, mode == AUTH_PASSWORD)
                layout.setRowVisible(self.agent_label, mode == AUTH_AGENT)

    def _auth_mode(self) -> str:
        if self.auth_key.isChecked():
            return AUTH_KEY
        if self.auth_password.isChecked():
            return AUTH_PASSWORD
        return AUTH_AGENT

    @staticmethod
    def _select_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index < 0:  # value written by hand in the file, keep it selectable
            combo.addItem(value, value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        h = self.host
        self.alias_edit.setText(h.alias)
        self.aliases_edit.setText(" ".join(h.aliases))
        self.hostname_edit.setText(h.hostname or "")
        self.user_edit.setText(h.user or "")
        self.port_edit.setText(str(h.port) if h.port else "")
        self.group_combo.setCurrentText(self.group or "")
        for path in h.identity_files:
            self._add_key(path)
        prefs = (h.preferred_authentications or "").lower()
        if h.identity_files:
            self.auth_key.setChecked(True)
        elif prefs.startswith("password") or prefs.startswith("keyboard-interactive"):
            self.auth_password.setChecked(True)
        elif self.original is None:
            self.auth_key.setChecked(True)
            default_key = next((k for k in find_private_keys() if "ed25519" in k), None) or next(iter(find_private_keys()), None)
            if default_key:
                self._add_key(default_key)
        else:
            self.auth_agent.setChecked(True)
        self.jump_combo.setCurrentText(h.proxy_jump or "")
        self.forward_agent.setCheckState(_tri_state(h.forward_agent))
        self.forward_x11.setCheckState(_tri_state(h.forward_x11))
        self.compression.setCheckState(_tri_state(h.compression))
        self.identities_only.setCheckState(_tri_state(h.identities_only))
        self.alive_interval.setText("" if h.server_alive_interval is None else str(h.server_alive_interval))
        self.alive_count.setText("" if h.server_alive_count_max is None else str(h.server_alive_count_max))
        self._select_data(self.strict_combo, h.strict_host_key_checking or "")
        self._select_data(self.add_keys_combo, h.add_keys_to_agent or "")
        self.prefs_edit.setText(h.preferred_authentications or "")
        self.proxy_command_edit.setText(h.proxy_command or "")
        for key, value in h.extra_options:
            self._add_row(self.extra_table, key, value)
        self.extra_table.setCurrentCell(-1, -1)
        for spec in h.local_forwards:
            self._add_forward("local", spec)
        for spec in h.remote_forwards:
            self._add_forward("remote", spec)
        for spec in h.dynamic_forwards:
            self._add_forward("dynamic", spec)
        self._auth_changed(0, True)
        self.alias_edit.setFocus()

    def _collect(self) -> SSHHost | None:
        """Build the host from the form; shows a message and returns None if invalid."""
        alias = self.alias_edit.text().strip()
        if not alias:
            self._invalid("Name is required.", self.alias_edit)
            return None
        aliases = split_arguments(self.aliases_edit.text())
        patterns = [alias, *aliases]
        if not self.is_defaults:
            if any(is_pattern(p) for p in patterns):
                self._invalid("Name and aliases cannot contain wildcards (*, ?, !). Use SSH Defaults for patterns.", self.alias_edit)
                return None
        clash = [p for p in patterns if p in self.existing]
        if clash:
            self._invalid(f'A host named "{clash[0]}" already exists.', self.alias_edit)
            return None
        port_text = self.port_edit.text().strip()
        port = _int_or_none(port_text)
        if port_text and (port is None or not 1 <= port <= 65535):
            self._invalid("Port must be between 1 and 65535.", self.port_edit)
            return None

        h = self.host
        h.alias, h.aliases = alias, aliases
        h.hostname = self.hostname_edit.text().strip() or None
        h.user = self.user_edit.text().strip() or None
        h.port = port
        mode = self._auth_mode()
        keys = [self.key_list.item(i).text() for i in range(self.key_list.count())]
        if mode == AUTH_KEY and self.key_combo.currentText().strip() and self.key_combo.currentText().strip() not in keys:
            keys.append(self.key_combo.currentText().strip())
        prefs = self.prefs_edit.text().strip() or None
        if mode == AUTH_KEY:
            if not keys and not self.is_defaults:
                self._invalid("Add at least one identity file, or choose another authentication method.", self.key_combo)
                return None
            missing = [k for k in keys if not expand_user_path(k).exists()]
            if missing and QMessageBox.question(
                self, "Identity file not found", "These files do not exist:\n\n" + "\n".join(missing) + "\n\nSave anyway?"
            ) != QMessageBox.StandardButton.Yes:
                return None
            h.identity_files = keys
            if prefs == PASSWORD_PREFS:
                prefs = None
        elif mode == AUTH_PASSWORD:
            h.identity_files = []
            prefs = prefs if prefs and prefs.startswith(("password", "keyboard-interactive")) else PASSWORD_PREFS
        else:
            h.identity_files = []
            if prefs == PASSWORD_PREFS:
                prefs = None
        h.preferred_authentications = prefs
        h.proxy_jump = self.jump_combo.currentText().strip() or None
        h.proxy_command = self.proxy_command_edit.text().strip() or None
        h.forward_agent = _from_tri_state(self.forward_agent.checkState())
        h.forward_x11 = _from_tri_state(self.forward_x11.checkState())
        h.compression = _from_tri_state(self.compression.checkState())
        h.identities_only = _from_tri_state(self.identities_only.checkState())
        h.server_alive_interval = _int_or_none(self.alive_interval.text())
        h.server_alive_count_max = _int_or_none(self.alive_count.text())
        h.strict_host_key_checking = self.strict_combo.currentData() or None
        h.add_keys_to_agent = self.add_keys_combo.currentData() or None

        extras: list[tuple[str, str]] = []
        for r in range(self.extra_table.rowCount()):
            key_item, value_item = self.extra_table.item(r, 0), self.extra_table.item(r, 1)
            key = key_item.text().strip() if key_item else ""
            value = value_item.text().strip() if value_item else ""
            if not key:
                continue
            if not key.isalnum():
                self._invalid(f'Invalid option name "{key}".', self.extra_table)
                return None
            if key.lower() in KNOWN_KEYWORDS:
                self._invalid(f'"{key}" has a dedicated field; edit it there.', self.extra_table)
                return None
            if key.lower() in ("host", "match"):
                self._invalid(f'"{key}" cannot be used inside a host block.', self.extra_table)
                return None
            extras.append((key, value))
        h.extra_options = extras

        h.local_forwards, h.remote_forwards, h.dynamic_forwards = [], [], []
        for r in range(self.forward_table.rowCount()):
            combo = self.forward_table.cellWidget(r, 0)
            kind = combo.currentText() if isinstance(combo, QComboBox) else "local"
            item = self.forward_table.item(r, 1)
            spec = item.text().strip() if item else ""
            if not spec:
                continue
            try:
                PortForwardSpec.parse(kind, spec)
            except ValueError as exc:
                self.tabs.setCurrentIndex(2)
                self._invalid(f"Invalid {kind} forward “{spec}”: {exc}", self.forward_table)
                return None
            {"local": h.local_forwards, "remote": h.remote_forwards, "dynamic": h.dynamic_forwards}[kind].append(spec)

        self.password = self.password_edit.text() if mode == AUTH_PASSWORD and self.password_edit.text() else None
        self.save_password = bool(self.password) and self.save_password_check.isChecked()
        self.group = self.group_combo.currentText().strip() or None
        return h

    def _invalid(self, message: str, widget: QWidget) -> None:
        QMessageBox.warning(self, "Invalid connection", message)
        page = widget
        while page is not None and self.tabs.indexOf(page) < 0:
            page = page.parentWidget()
        if page is not None:
            self.tabs.setCurrentWidget(page)
        widget.setFocus()

    def _save(self) -> None:
        if self._collect() is not None:
            self.accept()

    def _test(self) -> None:
        backup = copy.deepcopy(self.host)
        host = self._collect()
        if host is None:
            self.host = backup
            return
        if self.on_test:
            self.on_test(copy.deepcopy(host), self.password)
