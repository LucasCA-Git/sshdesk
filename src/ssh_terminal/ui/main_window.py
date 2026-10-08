"""Main application window: sidebar + tabbed terminals + menus + status bar."""

from __future__ import annotations

import base64
import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QByteArray, QFileSystemWatcher, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QCursor, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal import __app_name__
from ssh_terminal.errors import ConfigError, FriendlyError, describe_exception
from ssh_terminal.models.connection import ConnectionKind, ConnectionSpec, SessionState
from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.services import diagnostics_service
from ssh_terminal.services.app_context import AppContext
from ssh_terminal.services.connection_service import ConnectionService
from ssh_terminal.services.team_service import SyncResult, TeamApiError, TeamInfo, host_to_payload
from ssh_terminal.ssh.host_classifier import HostRole, git_remote_example, guess_role, host_role
from ssh_terminal.ssh.host_keys import KnownHostsStore, host_id_for
from ssh_terminal.terminal.backends.ssh_backend import SSHBackend
from ssh_terminal.terminal.terminal_session import TerminalSession
from ssh_terminal.terminal.terminal_widget import TerminalWidget
from ssh_terminal.ui.appearance_panel import AppearancePanel
from ssh_terminal.ui.connection_dialog import ConnectionDialog
from ssh_terminal.ui.connection_panel import ConnectionPanel
from ssh_terminal.ui.dialogs import confirm, mark_primary, show_error
from ssh_terminal.ui.file_pane import FilePane
from ssh_terminal.ui.grip_splitter import GripSplitter
from ssh_terminal.ui.info_dialogs import DiagnosticsDialog, ExternalChangeChoice, ExternalChangeDialog, WelcomeDialog, about_text
from ssh_terminal.ui.port_forward_dialog import PortForwardDialog
from ssh_terminal.ui.prompter import QtAuthPrompter
from ssh_terminal.ui.settings_dialog import SettingsDialog
from ssh_terminal.ui.team_dialogs import AccountDialog, ShareHostDialog, TeamsDialog
from ssh_terminal.ui.terminal_tabs import PaneBase, TerminalPane, TerminalTabs, state_color
from ssh_terminal.ui.test_connection_dialog import TestConnectionDialog
from ssh_terminal.ui.theme import app_icon, apply_theme, current_palette, icon
from ssh_terminal.ui.workers import run_async
from ssh_terminal.utils.paths import expand_user_path, get_log_dir
from ssh_terminal.utils.platform import default_editor_command

log = logging.getLogger(__name__)


@dataclass
class StartupOptions:
    connect: list[ConnectionSpec] = field(default_factory=list)
    config_override: str | None = None


class EmptyState(QWidget):
    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self.setObjectName("EmptyState")
        layout = QVBoxLayout(self)
        layout.addStretch(2)
        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo.setPixmap(app_icon().pixmap(96, 96))
        layout.addWidget(self.logo)
        title = QLabel("No open terminals")
        title.setObjectName("EmptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        subtitle = QLabel("Double-click a host in the sidebar, or start something new.")
        subtitle.setObjectName("EmptySubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)
        row = QHBoxLayout()
        row.addStretch(1)
        new = mark_primary(QPushButton("New Connection"))
        new.clicked.connect(window.new_connection)
        local = QPushButton("Local Terminal")
        local.clicked.connect(lambda: window.open_local())
        row.addWidget(new)
        row.addWidget(local)
        row.addStretch(1)
        layout.addSpacing(12)
        layout.addLayout(row)
        layout.addStretch(3)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, startup: StartupOptions | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.startup = startup or StartupOptions()
        self.setWindowTitle(__app_name__)
        self.setWindowIcon(app_icon())
        self.resize(1280, 800)
        self.setMinimumSize(640, 400)
        self.prompter = QtAuthPrompter(lambda: QApplication.activeModalWidget() or self)
        self.connections = ConnectionService(ctx.config, ctx.ssh, lambda: self.settings, self.prompter)
        self._handling_external_change = False
        self._actions: dict[str, QAction] = {}

        # Layout ---------------------------------------------------------
        self.sidebar = ConnectionPanel()
        self.tabs = TerminalTabs(self.settings, lambda: [s.name for s in self.connections.shells])
        self.empty = EmptyState(self)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.tabs)
        self.splitter = GripSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("MainSplitter")
        self.splitter.setHandleWidth(9)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.stack)
        self.appearance = AppearancePanel()
        self.appearance.hide()
        self.splitter.addWidget(self.appearance)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(0, True)  # drag the grip to the left edge to hide the hosts
        self.splitter.setCollapsible(1, False)
        self.splitter.handle_double_clicked.connect(lambda i: i == 1 and self.toggle_sidebar())
        self.setCentralWidget(self.splitter)

        self._build_status_bar()
        self._build_actions()
        self._build_menus()
        self._connect_signals()

        # File watcher ---------------------------------------------------
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._schedule_config_check)
        self.watcher.directoryChanged.connect(self._schedule_config_check)
        self._config_check_timer = QTimer(self)
        self._config_check_timer.setSingleShot(True)
        self._config_check_timer.setInterval(400)
        self._config_check_timer.timeout.connect(self._check_external_change)
        self._states_timer = QTimer(self)
        self._states_timer.setSingleShot(True)
        self._states_timer.setInterval(100)
        self._states_timer.timeout.connect(self._update_sidebar_states)
        self._team_timer = QTimer(self)
        self._team_timer.timeout.connect(lambda: self.sync_teams(quiet=True))
        self._team_task = None

        self.apply_settings(initial=True)
        self.reload_config(show_message=False)
        self._restore_geometry()
        QTimer.singleShot(0, self._after_show)

    # ------------------------------------------------------------------ #
    @property
    def settings(self):  # noqa: ANN201 - AppSettings
        return self.ctx.settings

    def save_settings(self) -> None:
        try:
            self.ctx.settings_service.save()
        except OSError as exc:
            log.error("Could not save settings: %s", exc)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self.status_state = QLabel("Disconnected")
        self.status_kind = QLabel("")
        self.status_name = QLabel("")
        self.status_endpoint = QLabel("")
        self.status_size = QLabel("")
        self.status_encoding = QLabel("UTF-8")
        for w in (self.status_state, self.status_kind, self.status_name, self.status_endpoint):
            bar.addWidget(w)
        self.status_account = QLabel("")
        bar.addPermanentWidget(self.status_account)
        bar.addPermanentWidget(self.status_size)
        bar.addPermanentWidget(self.status_encoding)

    def _action(self, key: str, text: str, slot, binding: str | None = None, icon_name: str | None = None) -> QAction:  # noqa: ANN001
        action = QAction(text, self)
        if icon_name:
            action.setIcon(icon(icon_name))
        action.triggered.connect(lambda _checked=False: slot())
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.addAction(action)
        self._actions[binding or key] = action
        return action

    def _build_actions(self) -> None:
        a = self._action
        a("new_connection", "New Connection...", self.new_connection, icon_name="plus")
        a("new_tab", "New Local Terminal", self.open_local, icon_name="terminal")
        a("import", "Import SSH Config", self.import_config)
        a("reload_config", "Reload SSH Config", lambda: self.reload_config(show_message=True), icon_name="refresh")
        a("settings", "Settings...", self.open_settings, icon_name="settings")
        a("exit", "Exit", self.close)
        a("copy", "Copy", self.copy)
        a("paste", "Paste", self.paste)
        a("select_all", "Select All", lambda: self._with_pane(lambda p: p.terminal.select_all()))
        a("toggle_sidebar", "Sidebar", self.toggle_sidebar)
        self._actions["toggle_sidebar"].setCheckable(True)
        a("focus_terminal", "Terminal", self.focus_terminal)
        a("split_right", "Split Right…", lambda: self.split_choose(Qt.Orientation.Horizontal), icon_name="split-right")
        a("split_down", "Split Down…", lambda: self.split_choose(Qt.Orientation.Vertical), icon_name="split-down")
        a("broadcast", "Broadcast Input to All Terminals", self.toggle_broadcast, icon_name="broadcast")
        self._actions["broadcast"].setCheckable(True)
        a("arrange_grid", "Arrange as Grid", self.arrange_grid, icon_name="grid")
        a("toggle_appearance", "Themes Panel", self.toggle_appearance, icon_name="palette")
        a("fullscreen", "Fullscreen", self.toggle_fullscreen)
        a("test_connection", "Test Connection", self.test_current_connection)
        a("reconnect", "Reconnect", lambda: self._with_pane(lambda p: p.reconnect()))
        a("edit_connection", "Edit Connection...", self.edit_current_connection)
        a("port_forwarding", "Port Forwarding...", self.open_port_forwarding)
        a("open_config", "Open SSH Config", lambda: self.open_config_file(self.ctx.config.path))
        a("config_dir", "SSH Config Directory", self.open_config_dir, icon_name="folder")
        a("clear", "Clear", lambda: self._with_pane(lambda p: p.terminal.clear()))
        a("reset", "Reset", lambda: self._with_pane(lambda p: p.terminal.reset()))
        a("font_increase", "Increase Font", lambda: self._with_pane(lambda p: p.terminal.zoom(1)))
        a("font_decrease", "Decrease Font", lambda: self._with_pane(lambda p: p.terminal.zoom(-1)))
        a("font_reset", "Reset Font", lambda: self._with_pane(lambda p: p.terminal.reset_zoom()))
        a("close_tab", "Close Tab / Pane", self.close_current)
        a("reopen_tab", "Reopen Closed Tab", self.reopen_tab)
        a("next_tab", "Next Tab", lambda: self.tabs.next_tab(1))
        a("prev_tab", "Previous Tab", lambda: self.tabs.next_tab(-1))
        a("search", "Search Connections", self.focus_search)
        a("documentation", "Documentation", self.show_documentation)
        a("about", f"About {__app_name__}", self.show_about)
        a("diagnostics", "Diagnostics", self.show_diagnostics)
        a("open_logs", "Open Log Folder", lambda: self._open_path(get_log_dir()))
        a("sign_in", "Sign In…", lambda: self.sign_in(create=False))
        a("create_account", "Create Account…", lambda: self.sign_in(create=True))
        a("teams", "Teams…", self.open_teams)
        a("sync_teams", "Sync Team Hosts", lambda: self.sync_teams(force=True))
        a("sign_out", "Sign Out", self.sign_out)

    def _build_menus(self) -> None:
        bar = self.menuBar()
        acts = self._actions
        file_menu = bar.addMenu("&File")
        file_menu.addAction(acts["new_connection"])
        file_menu.addAction(acts["new_tab"])
        self.local_menu = file_menu.addMenu("New Local Terminal With")
        self.local_menu.aboutToShow.connect(self._fill_local_menu)
        file_menu.addSeparator()
        file_menu.addAction(acts["import"])
        file_menu.addAction(acts["reload_config"])
        file_menu.addSeparator()
        file_menu.addAction(acts["settings"])
        file_menu.addSeparator()
        file_menu.addAction(acts["exit"])

        edit_menu = bar.addMenu("&Edit")
        for key in ("copy", "paste", "select_all"):
            edit_menu.addAction(acts[key])
        edit_menu.addSeparator()
        edit_menu.addAction(acts["search"])

        view_menu = bar.addMenu("&View")
        view_menu.addAction(acts["toggle_sidebar"])
        view_menu.addAction(acts["focus_terminal"])
        view_menu.addSeparator()
        view_menu.addAction(acts["split_right"])
        view_menu.addAction(acts["split_down"])
        view_menu.addAction(acts["arrange_grid"])
        view_menu.addAction(acts["broadcast"])
        view_menu.addSeparator()
        view_menu.addAction(acts["toggle_appearance"])
        view_menu.addSeparator()
        view_menu.addAction(acts["fullscreen"])

        ssh_menu = bar.addMenu("&SSH")
        for key in ("new_connection", "test_connection", "reconnect", "edit_connection", "port_forwarding"):
            ssh_menu.addAction(acts[key])
        ssh_menu.addSeparator()
        ssh_menu.addAction(acts["open_config"])
        ssh_menu.addAction(acts["config_dir"])

        term_menu = bar.addMenu("&Terminal")
        for key in ("new_tab", "close_tab", "reopen_tab", "next_tab", "prev_tab"):
            term_menu.addAction(acts[key])
        term_menu.addSeparator()
        for key in ("clear", "reset", "broadcast"):
            term_menu.addAction(acts[key])
        term_menu.addSeparator()
        for key in ("font_increase", "font_decrease", "font_reset"):
            term_menu.addAction(acts[key])

        account_menu = bar.addMenu("&Account")
        for key in ("sign_in", "create_account"):
            account_menu.addAction(acts[key])
        account_menu.addSeparator()
        for key in ("teams", "sync_teams"):
            account_menu.addAction(acts[key])
        account_menu.addSeparator()
        account_menu.addAction(acts["sign_out"])
        account_menu.aboutToShow.connect(self._update_account_ui)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(acts["documentation"])
        help_menu.addAction(acts["diagnostics"])
        help_menu.addAction(acts["open_logs"])
        help_menu.addSeparator()
        help_menu.addAction(acts["about"])

    def _fill_local_menu(self) -> None:
        self.local_menu.clear()
        for shell in self.connections.shells:
            self.local_menu.addAction(icon("terminal"), shell.name, lambda n=shell.name: self.open_local(n))

    def _connect_signals(self) -> None:
        sb = self.sidebar
        sb.connect_requested.connect(self.connect_alias)
        sb.edit_requested.connect(self.edit_connection)
        sb.edit_defaults_requested.connect(self.edit_connection)
        sb.delete_requested.connect(self.delete_connection)
        sb.duplicate_requested.connect(self.duplicate_connection)
        sb.rename_requested.connect(self.rename_connection)
        sb.copy_command_requested.connect(self.copy_ssh_command)
        sb.open_config_requested.connect(self.open_config_for)
        sb.test_requested.connect(self.test_connection)
        sb.favorite_toggled.connect(self.toggle_favorite)
        sb.group_requested.connect(self.new_group_for)
        sb.group_set.connect(self.set_group)
        sb.role_set.connect(self.set_role)
        sb.share_requested.connect(self.share_host)
        sb.share_group_requested.connect(self.share_group)
        sb.team_edit_requested.connect(self.edit_team_host)
        sb.team_delete_requested.connect(self.delete_team_host)
        sb.git_test_requested.connect(self.test_git_host)
        sb.copy_git_remote_requested.connect(self.copy_git_remote)
        sb.new_requested.connect(self.new_connection)
        sb.local_requested.connect(lambda: self.open_local())
        sb.settings_requested.connect(self.open_settings)
        sb.import_requested.connect(self.import_config)
        sb.reload_requested.connect(lambda: self.reload_config(show_message=True))
        sb.section_toggled.connect(self._section_toggled)

        t = self.tabs
        t.current_pane_changed.connect(self._current_pane_changed)
        t.pane_state_changed.connect(self._update_status)
        t.new_tab_requested.connect(lambda: self.open_local())
        t.new_connection_requested.connect(self.new_connection)
        t.local_shell_requested.connect(self.open_local)
        t.pane_context_menu.connect(self._pane_menu)
        t.edit_connection_requested.connect(self.edit_connection)
        t.duplicate_requested.connect(lambda p: self.open_spec(ConnectionSpec.from_dict(p.spec.to_dict())))
        t.pane_action.connect(self._pane_action)
        sb.split_view_requested.connect(self.open_split_view)
        sb.open_split_requested.connect(self.split_alias)
        sb.files_requested.connect(self.files_alias)
        t.files_requested.connect(lambda: self.open_files(None))
        t.broadcast_changed.connect(self._broadcast_changed)
        self.appearance.scheme_chosen.connect(self._scheme_chosen)
        self.appearance.font_changed.connect(self._font_changed)
        self.appearance.close_requested.connect(self.toggle_appearance)
        # Tab bar corner: [ + ] [ broadcast ] [ grid ] [ themes ]
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 6, 0)
        corner_layout.setSpacing(2)
        self.grid_button = QToolButton()
        self.grid_button.setToolTip("Arrange terminals as grid")
        self.grid_button.clicked.connect(self.arrange_grid)
        self.broadcast_button = QToolButton()
        self.broadcast_button.setToolTip("Broadcast input: what you type goes to every terminal of this tab")
        self.broadcast_button.setCheckable(True)
        self.broadcast_button.clicked.connect(self.toggle_broadcast)
        self.theme_button = QToolButton()
        self.theme_button.setToolTip("Themes panel")
        self.theme_button.setCheckable(True)
        self.theme_button.clicked.connect(self.toggle_appearance)
        for button in (t.plus, self.broadcast_button, self.grid_button, self.theme_button):
            corner_layout.addWidget(button)
        t.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        # Left corner: show/hide the hosts sidebar
        left = QWidget()
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(6, 0, 2, 0)
        self.sidebar_button = QToolButton()
        self.sidebar_button.setObjectName("SidebarToggle")
        self.sidebar_button.setToolTip("Show/hide hosts (Ctrl+Shift+B)")
        self.sidebar_button.setCheckable(True)
        self.sidebar_button.clicked.connect(self.toggle_sidebar)
        left_layout.addWidget(self.sidebar_button)
        t.setCornerWidget(left, Qt.Corner.TopLeftCorner)
        t.session_closed.connect(lambda _p, _i: self._states_timer.start())
        self.splitter.splitterMoved.connect(lambda *_: self._sidebar_moved())

    # ------------------------------------------------------------------ #
    # Settings / theme
    # ------------------------------------------------------------------ #
    def apply_settings(self, initial: bool = False) -> None:
        s = self.settings
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, s.theme)
        self.setWindowOpacity(max(0.5, s.window_opacity / 100))
        reserved: set[int] = set()
        for key, action in self._actions.items():
            binding = s.keybindings.get(key, "")
            sequence = QKeySequence(binding) if binding else QKeySequence()
            action.setShortcut(sequence)
            if not sequence.isEmpty():
                reserved.add(sequence[0].toCombined())
        TerminalWidget.reserved_shortcuts = reserved
        self._actions["toggle_sidebar"].setChecked(s.sidebar_visible)
        if not initial and self._sidebar_open() != s.sidebar_visible:
            self._set_sidebar_open(s.sidebar_visible)
        self.sidebar.refresh_icons()
        self.tabs.refresh_icons()
        if hasattr(self, "grid_button"):
            self.grid_button.setIcon(icon("grid", current_palette().muted))
            self.theme_button.setIcon(icon("palette", current_palette().muted))
            page = self.tabs.current_page()
            self._broadcast_changed(bool(page and page.broadcast))
            self._sync_sidebar_button()
        self.empty.logo.setPixmap(app_icon().pixmap(96, 96))
        for pane in self.tabs.all_panes():
            pane.apply_settings(s)
        if not initial:
            changed = self.ctx.apply_settings(self.startup.config_override)
            if changed:
                self.reload_config(show_message=False)
            self._refresh_sidebar()
        self._watch_config()

    def open_settings(self) -> None:
        dlg = SettingsDialog(
            self.settings,
            [s.name for s in self.connections.shells],
            self.ctx.credentials.clear_all,
            self.ctx.credentials.backend_name(),
            self.show_diagnostics,
            self,
        )
        if dlg.exec():
            new = dlg.result_settings
            current = self.settings
            # keep UI metadata that may have changed while the dialog was open
            new.favorites, new.recent_connections, new.groups = current.favorites, current.recent_connections, current.groups
            self.ctx.settings_service.settings = new
            self.save_settings()
            self.apply_settings()

    # ------------------------------------------------------------------ #
    # SSH config
    # ------------------------------------------------------------------ #
    def reload_config(self, show_message: bool = False) -> None:
        try:
            self.ctx.config.reload()
        except (OSError, ConfigError) as exc:
            show_error(self, describe_exception(exc))
            return
        self._refresh_sidebar()
        self._watch_config()
        if show_message:
            count = len(self.ctx.config.hosts())
            self.statusBar().showMessage(f"SSH configuration reloaded — {count} host(s).", 4000)

    def _refresh_sidebar(self) -> None:
        cfg = self.ctx.config
        teams = self.ctx.teams
        self.sidebar.populate(
            cfg.hosts(),
            cfg.wildcard_hosts(),
            self.settings,
            team_of=lambda h: teams.team_for_path(h.source_file),
            editable_teams=[t for t in teams.teams if t.can_edit] if teams.signed_in else [],
            team_group_of=teams.group_of,
        )
        self._update_sidebar_states()

    def import_config(self) -> None:
        cfg = self.ctx.config
        if not cfg.exists:
            if confirm(self, "Import SSH Config", f"No SSH configuration found at\n{cfg.path}\n\nWould you like to create one?", "Create SSH Config"):
                self._create_config()
            return
        self.reload_config()
        count = len(cfg.hosts())
        QMessageBox.information(self, "Import SSH Config", f"SSH configuration loaded.\n\n{count} host{'s' if count != 1 else ''} found.\n\n{cfg.path}")

    def _create_config(self) -> None:
        try:
            self.ctx.config.create_config_file()
        except OSError as exc:
            show_error(self, describe_exception(exc))
            return
        self.reload_config()
        self.statusBar().showMessage(f"Created {self.ctx.config.path}", 5000)

    def _watch_config(self) -> None:
        if self.watcher.files():
            self.watcher.removePaths(self.watcher.files())
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        if not self.settings.watch_config:
            return
        paths = {str(p) for p in self.ctx.config.watched_files() if p.exists()}
        dirs = {str(p.parent) for p in self.ctx.config.watched_files() if p.parent.exists()}
        if paths:
            self.watcher.addPaths(sorted(paths))
        if dirs:
            self.watcher.addPaths(sorted(dirs))

    def _schedule_config_check(self, _path: str) -> None:
        self._config_check_timer.start()

    def _check_external_change(self) -> None:
        if self._handling_external_change:
            return
        changed = self.ctx.config.changed_files()
        self._watch_config()  # atomic replaces drop the watch: re-add
        if not changed:
            return
        log.info("SSH config changed externally: %s", ", ".join(map(str, changed)))
        if self.settings.auto_reload_config:
            self.reload_config(show_message=True)
            return
        self._handling_external_change = True
        try:
            path = changed[0]
            old = self.ctx.config.known_text(path)
            try:
                new = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            except OSError:
                new = ""
            dlg = ExternalChangeDialog(str(path), old, new, self)
            dlg.exec()
            if dlg.choice is ExternalChangeChoice.RELOAD:
                self.reload_config(show_message=True)
            else:
                self.ctx.config.acknowledge_external_change()
        finally:
            self._handling_external_change = False

    # ------------------------------------------------------------------ #
    # Host CRUD
    # ------------------------------------------------------------------ #
    def _existing_aliases(self) -> set[str]:
        return {p for h in self.ctx.config.config.hosts() for p in h.patterns}

    def _run_config_change(self, func, *args) -> bool:  # noqa: ANN001
        try:
            func(*args)
        except (ConfigError, OSError) as exc:
            show_error(self, describe_exception(exc))
            return False
        return True

    def _dialog_test(self, host: SSHHost, password: str | None) -> None:
        try:
            resolved = self.ctx.ssh.resolve_unsaved(host)
        except (OSError, ConfigError) as exc:
            show_error(self, describe_exception(exc))
            return
        TestConnectionDialog(self.ctx.ssh, resolved, self.prompter, password, QApplication.activeModalWidget() or self).exec()

    def _store_password(self, host: SSHHost, dlg: ConnectionDialog) -> None:
        if dlg.save_password and dlg.password and self.settings.allow_keyring:
            try:
                resolved = self.ctx.ssh.resolve(host.alias)
            except (OSError, ConfigError) as exc:
                log.warning("Could not resolve %s to save password: %s", host.alias, exc)
                return
            self.ctx.credentials.set_password(resolved.user, resolved.hostname, resolved.port, dlg.password)

    def new_connection(self) -> None:
        if not self.ctx.config.exists:
            if not confirm(self, "No SSH config", f"{self.ctx.config.path} does not exist yet.\n\nCreate it now?", "Create"):
                return
            self._create_config()
        dlg = ConnectionDialog(
            None,
            self._existing_aliases(),
            sorted(self.settings.groups),
            on_test=self._dialog_test,
            keyring_available=self.ctx.credentials.available and self.settings.allow_keyring,
            parent=self,
        )
        if not dlg.exec():
            return
        host = dlg.host
        if not self._run_config_change(self.ctx.config.add_host, host):
            return
        if dlg.group:
            self.settings.set_group(host.alias, dlg.group)
        self.save_settings()
        self._store_password(host, dlg)
        self._refresh_sidebar()
        self.statusBar().showMessage(f"Connection “{host.alias}” saved to {self.ctx.config.path}", 5000)

    def edit_connection(self, alias: str) -> None:
        if self._team_of_alias(alias) is not None:
            self.edit_team_host(alias)
            return
        host = self.ctx.config.get_host(alias)
        if host is None:
            show_error(self, FriendlyError("Host not found", f"“{alias}” is no longer in the SSH config. Reload and try again."))
            self.reload_config()
            return
        dlg = ConnectionDialog(
            host,
            self._existing_aliases(),
            sorted(self.settings.groups),
            group=self.settings.group_of(alias),
            on_test=self._dialog_test,
            keyring_available=self.ctx.credentials.available and self.settings.allow_keyring,
            parent=self,
        )
        if not dlg.exec():
            return
        new_host = dlg.host
        if not self._run_config_change(self.ctx.config.update_host, alias, new_host):
            return
        if new_host.alias != alias:
            self.settings.rename_alias(alias, new_host.alias)
            for pane in self.tabs.all_panes():
                if pane.spec.kind is ConnectionKind.SSH and pane.spec.alias == alias:
                    pane.spec.alias = new_host.alias
        if not host.is_wildcard:
            self.settings.set_group(new_host.alias, dlg.group)
        self.save_settings()
        self._store_password(new_host, dlg)
        self._refresh_sidebar()
        self.statusBar().showMessage(f"“{new_host.alias}” updated (backup saved).", 5000)

    def edit_current_connection(self) -> None:
        pane = self.tabs.current_pane()
        if pane and pane.spec.kind is ConnectionKind.SSH and pane.spec.alias:
            self.edit_connection(pane.spec.alias)
        elif self.sidebar.selected_alias():
            self.edit_connection(self.sidebar.selected_alias())

    def delete_connection(self, alias: str) -> None:
        if self._team_of_alias(alias) is not None:
            self.delete_team_host(alias)
            return
        if not confirm(
            self,
            "Remove connection",
            f"Are you sure you want to remove \"{alias}\"\nfrom your SSH configuration?",
            "Remove",
            danger=True,
        ):
            return
        if self._run_config_change(self.ctx.config.remove_host, alias):
            self.settings.forget_alias(alias)
            self.save_settings()
            self._refresh_sidebar()
            self.statusBar().showMessage(f"“{alias}” removed (backup saved).", 5000)

    def duplicate_connection(self, alias: str) -> None:
        try:
            dup = self.ctx.config.duplicate_host(alias)
        except (ConfigError, OSError) as exc:
            show_error(self, describe_exception(exc))
            return
        group = self.settings.group_of(alias)
        if group:
            self.settings.set_group(dup.alias, group)
            self.save_settings()
        self._refresh_sidebar()
        self.edit_connection(dup.alias)

    def rename_connection(self, alias: str) -> None:
        if self._team_of_alias(alias) is not None:
            QMessageBox.information(self, "Rename", "Team hosts are renamed by a team admin (Edit in Team).")
            return
        new, ok = QInputDialog.getText(self, "Rename connection", "New name:", text=alias)
        new = new.strip()
        if not ok or not new or new == alias:
            return
        if any(c.isspace() for c in new) or any(c in new for c in '*?!"#'):
            show_error(self, FriendlyError("Invalid name", "Names cannot contain spaces, quotes, # or wildcards."))
            return
        if self._run_config_change(self.ctx.config.rename_host, alias, new):
            self.settings.rename_alias(alias, new)
            for pane in self.tabs.all_panes():
                if pane.spec.alias == alias:
                    pane.spec.alias = new
            self.save_settings()
            self._refresh_sidebar()

    def toggle_favorite(self, alias: str) -> None:
        self.settings.toggle_favorite(alias)
        self.save_settings()
        self._refresh_sidebar()

    def new_group_for(self, alias: str) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Group name (visual only, not saved in the SSH config):")
        if ok and name.strip():
            self.set_group(alias, name.strip())

    def set_role(self, alias: str, role: str) -> None:
        """Manual override of the server/Git classification (UI metadata only)."""
        host = self.ctx.config.get_host(alias)
        if host is not None and guess_role(host).value == role:
            self.settings.host_roles.pop(alias, None)  # back to automatic
        else:
            self.settings.host_roles[alias] = role
        self.save_settings()
        self._refresh_sidebar()

    def is_git_host(self, alias: str) -> bool:
        host = self.ctx.config.get_host(alias)
        return host is not None and host_role(host, self.settings.host_roles) is HostRole.GIT

    def test_git_host(self, alias: str) -> None:
        host = self.ctx.config.get_host(alias)
        if host is None:
            return
        try:
            resolved = self.ctx.ssh.resolve(alias)
        except (OSError, ConfigError) as exc:
            show_error(self, describe_exception(exc))
            return
        hint = (f"<b>{alias}</b> is a Git/service endpoint: SSH is used only to authenticate, there is no shell.<br>"
                f"Use it in Git remotes: <code>{git_remote_example(host)}</code>")
        TestConnectionDialog(self.ctx.ssh, resolved, self.prompter, None, self, hint=hint).exec()

    def copy_git_remote(self, alias: str) -> None:
        host = self.ctx.config.get_host(alias)
        if host is not None:
            text = git_remote_example(host)
            QGuiApplication.clipboard().setText(text)
            self.statusBar().showMessage(f"Copied: {text}", 4000)

    def set_group(self, alias: str, group: str | None) -> None:
        self.settings.set_group(alias, group)
        self.save_settings()
        self._refresh_sidebar()

    def _section_toggled(self, key: str, expanded: bool) -> None:
        collapsed = set(self.settings.collapsed_sections)
        if expanded:
            collapsed.discard(key)
        else:
            collapsed.add(key)
        self.settings.collapsed_sections = sorted(collapsed)
        self.save_settings()

    def copy_ssh_command(self, alias: str, full: bool) -> None:
        if not full:
            command = f"ssh {shlex.quote(alias)}"
        else:
            try:
                r = self.ctx.ssh.resolve(alias)
            except (OSError, ConfigError) as exc:
                show_error(self, describe_exception(exc))
                return
            parts = ["ssh"]
            if r.port != 22:
                parts += ["-p", str(r.port)]
            if r.identity_files_explicit:
                for identity in r.identity_files:
                    parts += ["-i", identity]
            if r.proxy_jump:
                parts += ["-J", r.proxy_jump]
            if r.forward_agent:
                parts.append("-A")
            parts.append(f"{r.user}@{r.hostname}")
            command = " ".join(shlex.quote(p) if not p.startswith("~") else p for p in parts)
        QGuiApplication.clipboard().setText(command)
        self.statusBar().showMessage(f"Copied: {command}", 4000)

    def open_config_for(self, alias: str) -> None:
        host = self.ctx.config.get_host(alias)
        path = host.source_file if host and host.source_file else self.ctx.config.path
        self.open_config_file(path)

    def open_config_file(self, path: Path) -> None:
        if not path.exists():
            if confirm(self, "Open SSH Config", f"{path} does not exist.\n\nCreate it?", "Create"):
                self._create_config()
            else:
                return
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            return
        command = default_editor_command(path)
        if command:
            try:
                subprocess.Popen(command, close_fds=True)  # noqa: S603 - fixed editor command
                return
            except OSError as exc:
                log.warning("Could not start editor: %s", exc)
        show_error(self, FriendlyError("Cannot open file", f"No application is associated with:\n{path}"))

    def open_config_dir(self) -> None:
        self._open_path(self.ctx.config.path.parent)

    def _open_path(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ------------------------------------------------------------------ #
    # Connections / tabs
    # ------------------------------------------------------------------ #
    def connect_alias(self, alias: str, new_tab: bool = True) -> None:
        if not new_tab and self.is_git_host(alias):
            self.test_git_host(alias)  # Git endpoints have no shell to open
            return
        if not new_tab:
            for page in self.tabs.pages():
                for pane in page.panes():
                    if pane.spec.alias == alias and pane.state in (SessionState.CONNECTED, SessionState.CONNECTING):
                        self.tabs.setCurrentWidget(page)
                        pane.terminal.setFocus()
                        return
        self.open_spec(ConnectionSpec.ssh(alias))

    def open_local(self, shell: str = "") -> None:
        self.open_spec(ConnectionSpec.local(shell))

    def _make_pane(self, spec: ConnectionSpec) -> TerminalPane:
        session = TerminalSession(
            spec,
            self.connections.backend_factory(spec),
            auto_reconnect=self.settings.auto_reconnect,
            max_attempts=self.settings.reconnect_attempts,
        )
        if spec.kind is ConnectionKind.LOCAL and not spec.shell:
            spec.shell = self.connections.shell_profile().name
        pane = TerminalPane(spec, session, self.settings)
        pane.state_changed.connect(self._pane_state_changed)
        if spec.theme:
            pane.set_theme(spec.theme)
        pane.terminal.size_changed.connect(lambda c, r, p=pane: self._size_changed(p, c, r))
        return pane

    def open_spec(self, spec: ConnectionSpec, split: Qt.Orientation | None = None) -> TerminalPane:
        pane = self._make_pane(spec)
        current = self.tabs.current_item()
        if split is not None and current is not None:
            self.tabs.split_pane(current, pane, split)
        else:
            self.tabs.add_pane_tab(pane)
        self.stack.setCurrentWidget(self.tabs)
        QTimer.singleShot(0, pane.start)
        return pane

    def split(self, orientation: Qt.Orientation) -> None:
        pane = self.tabs.current_pane()
        if pane is None:
            self.open_local()
            return
        self.open_spec(ConnectionSpec.from_dict(pane.spec.to_dict()), split=orientation)

    def close_current(self) -> None:
        pane = self.tabs.current_pane()
        if pane is not None:
            self.tabs.close_pane(pane)

    def reopen_tab(self) -> None:
        specs = self.tabs.pop_closed()
        if not specs:
            return
        first = self.open_spec(specs[0])
        for spec in specs[1:]:
            self.tabs.split_pane(first, self._make_pane(spec), Qt.Orientation.Horizontal)
            pane = self.tabs.current_pane()
            if pane is not None:
                QTimer.singleShot(0, pane.start)

    def _pane_state_changed(self, pane: TerminalPane) -> None:
        if pane.state is SessionState.CONNECTED and pane.spec.kind is ConnectionKind.SSH and pane.spec.alias:
            self.settings.add_recent(pane.spec.alias)
            self.save_settings()
            QTimer.singleShot(0, self._refresh_sidebar)
        self._states_timer.start()
        if pane is self.tabs.current_pane():
            self._update_status(pane)

    def _update_sidebar_states(self) -> None:
        priority = {SessionState.CONNECTED: 3, SessionState.CONNECTING: 2, SessionState.FAILED: 1, SessionState.DISCONNECTED: 1}
        best: dict[str, SessionState] = {}
        for pane in self.tabs.all_panes():
            if pane.spec.kind is not ConnectionKind.SSH or not pane.spec.alias:
                continue
            state = pane.state
            if state is SessionState.IDLE:
                continue
            old = best.get(pane.spec.alias)
            if old is None or priority[state] > priority[old]:
                best[pane.spec.alias] = state
        self.sidebar.set_connection_states({a: s.value for a, s in best.items()})
        if not self.tabs.count():
            self.stack.setCurrentWidget(self.empty)

    def _current_pane_changed(self, pane: TerminalPane | None) -> None:
        if self.appearance.isVisible() and pane is not None:
            self.appearance.load(pane.terminal.scheme_override or self.settings.color_scheme,
                                 self.settings.font_family, self.settings.font_size)
        if pane is None and self.tabs.count() == 0:
            self.stack.setCurrentWidget(self.empty)
        self._update_status(pane)
        self._states_timer.start()

    def _size_changed(self, pane: TerminalPane, cols: int, rows: int) -> None:
        if pane is self.tabs.current_pane():
            self.status_size.setText(f"{cols}×{rows}")

    def _update_status(self, pane: TerminalPane | None) -> None:
        pal = current_palette()
        if pane is None:
            self.status_state.setText(f"<span style='color:{pal.muted}'>●</span> Disconnected")
            for w in (self.status_kind, self.status_name, self.status_endpoint, self.status_size):
                w.setText("")
            self.setWindowTitle(__app_name__)
            return
        state = pane.state
        label = {
            SessionState.CONNECTED: "Connected",
            SessionState.CONNECTING: "Connecting",
            SessionState.FAILED: "Failed",
            SessionState.DISCONNECTED: "Disconnected",
            SessionState.IDLE: "Starting",
        }[state]
        self.status_state.setText(f"<span style='color:{state_color(state)}'>●</span> {label}")
        self.status_kind.setText("SSH" if pane.spec.kind is ConnectionKind.SSH else "Local")
        self.status_name.setText(pane.title)
        backend = pane.session.backend
        endpoint = backend.description if backend is not None else ""
        self.status_endpoint.setText("" if endpoint == pane.title else endpoint)
        self.status_encoding.setText(backend.encoding if backend is not None else "UTF-8")
        cols, rows = pane.terminal.grid_size()
        self.status_size.setText(f"{cols}×{rows}")
        self.setWindowTitle(f"{pane.title} — {__app_name__}")

    def _with_pane(self, func) -> None:  # noqa: ANN001
        pane = self.tabs.current_pane()
        if pane is not None:
            func(pane)

    def copy(self) -> None:
        self._with_pane(lambda p: p.terminal.copy())

    def paste(self) -> None:
        self._with_pane(lambda p: p.terminal.paste())

    def focus_terminal(self) -> None:
        self._with_pane(lambda p: p.terminal.setFocus())

    def focus_search(self) -> None:
        if not self.sidebar.isVisible():
            self.toggle_sidebar()
        self.sidebar.focus_search()

    # -- hosts sidebar: collapsed (width 0) instead of hidden, so its grip stays to pull it back
    def _sidebar_open(self) -> bool:
        return self.splitter.sizes()[0] > 0

    def _set_sidebar_open(self, open_: bool) -> None:
        sizes = self.splitter.sizes()
        if open_:
            width = max(220, self.settings.sidebar_width)
            sizes[1] = max(300, sizes[1] - (width - sizes[0]))
            sizes[0] = width
        else:
            if sizes[0] > 0:
                self.settings.sidebar_width = sizes[0]
            sizes[1] += sizes[0]
            sizes[0] = 0
        self.splitter.setSizes(sizes)
        self.settings.sidebar_visible = open_
        self._actions["toggle_sidebar"].setChecked(open_)
        self._sync_sidebar_button()

    def toggle_sidebar(self) -> None:
        self._set_sidebar_open(not self._sidebar_open())
        self.save_settings()

    def _sidebar_moved(self) -> None:
        self._remember_sidebar_width()
        open_ = self._sidebar_open()
        if open_ != self.settings.sidebar_visible:
            self.settings.sidebar_visible = open_
            self._actions["toggle_sidebar"].setChecked(open_)
            self._sync_sidebar_button()

    def _sync_sidebar_button(self) -> None:
        if hasattr(self, "sidebar_button"):
            visible = self.settings.sidebar_visible
            self.sidebar_button.setChecked(visible)
            pal = current_palette()
            self.sidebar_button.setIcon(icon("sidebar", pal.accent if visible else pal.muted))

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _pane_menu(self, pane: TerminalPane, pos: QPoint) -> None:
        menu = QMenu(self)
        copy = menu.addAction("Copy", pane.terminal.copy)
        copy.setEnabled(pane.terminal.has_selection())
        menu.addAction("Paste", pane.terminal.paste)
        menu.addAction("Select All", pane.terminal.select_all)
        menu.addSeparator()
        menu.addAction("Clear", pane.terminal.clear)
        menu.addAction("Reset", pane.terminal.reset)
        menu.addSeparator()
        right = menu.addMenu(icon("split-right"), "Split Right")
        self._fill_split_menu(right, pane, Qt.Orientation.Horizontal)
        down = menu.addMenu(icon("split-down"), "Split Down")
        self._fill_split_menu(down, pane, Qt.Orientation.Vertical)
        page = self.tabs.current_page()
        bc = menu.addAction(icon("broadcast"), "Broadcast Input to All Terminals", self.toggle_broadcast)
        bc.setCheckable(True)
        bc.setChecked(bool(page and page.broadcast))
        menu.addAction("Close Pane", lambda: self.tabs.close_pane(pane))
        menu.addSeparator()
        files_spec = ConnectionSpec.from_dict(pane.spec.to_dict())
        menu.addAction(icon("folder"), "Open Files (SFTP) to the Right" if pane.spec.kind is ConnectionKind.SSH
                       else "Open Local Files to the Right",
                       lambda: self.open_files(files_spec, "right", ref=pane))
        menu.addAction("Reconnect", pane.reconnect)
        if pane.spec.kind is ConnectionKind.SSH:
            forward = menu.addAction("Port Forwarding...", lambda: self.open_port_forwarding(pane))
            forward.setEnabled(pane.state is SessionState.CONNECTED)
            if pane.spec.alias:
                menu.addAction("Edit Connection...", lambda: self.edit_connection(pane.spec.alias))
        menu.exec(pos)

    def _pane_action(self, pane: PaneBase, action: str) -> None:
        if action == "split_right":
            self._split_chooser(pane, Qt.Orientation.Horizontal, QCursor.pos())
        elif action == "split_down":
            self._split_chooser(pane, Qt.Orientation.Vertical, QCursor.pos())
        elif action == "broadcast":
            self.tabs.toggle_broadcast(self.tabs._page_of(pane))
            pane.focus_target().setFocus()
        elif action == "close":
            self.tabs.close_pane(pane)

    # -- split with another terminal / broadcast --------------------------- #
    def _shell_hosts(self) -> list:  # noqa: ANN201 - list[SSHHost]
        hosts = [h for h in self.ctx.config.hosts() if not self.is_git_host(h.alias)]
        hosts.sort(key=lambda h: (not self.settings.is_favorite(h.alias), h.alias.lower()))
        return hosts

    def _host_actions(self, menu: QMenu, slot) -> None:  # noqa: ANN001 - slot(alias)
        target = menu
        hosts = self._shell_hosts()
        for i, host in enumerate(hosts):
            if i == 20:
                target = menu.addMenu(f"More Hosts ({len(hosts) - 20})")
            name = "star-filled" if self.settings.is_favorite(host.alias) else "server"
            label = host.alias if not host.hostname or host.hostname == host.alias else f"{host.alias}   ·   {host.hostname}"
            target.addAction(icon(name), label, lambda a=host.alias: slot(a))

    def _fill_split_menu(self, menu: QMenu, pane: PaneBase, orientation: Qt.Orientation) -> None:
        """Choices for the new pane: same connection, a local shell, any SSH host, or files (SFTP)."""
        is_files = isinstance(pane, FilePane)
        if pane.spec is not None:
            spec = ConnectionSpec.from_dict(pane.spec.to_dict())
            same = menu.addAction(icon("split-right" if orientation is Qt.Orientation.Horizontal else "split-down"),
                                  f"Same Connection ({pane.title})",
                                  (lambda: self.split_files_with(pane, spec, orientation)) if is_files
                                  else (lambda: self.split_with(pane, spec, orientation)))
            menu.setDefaultAction(same)
        files = menu.addMenu(icon("folder"), "Files (SFTP)")
        files.addAction(icon("terminal"), "This Computer",
                        lambda: self.split_files_with(pane, ConnectionSpec.local(), orientation))
        files.addSeparator()
        self._host_actions(files, lambda a: self.split_files_with(pane, ConnectionSpec.ssh(a), orientation))
        menu.addSection("Local terminal")
        for shell in self.connections.shells:
            menu.addAction(icon("terminal"), shell.name,
                           lambda n=shell.name: self.split_with(pane, ConnectionSpec.local(n), orientation))
        if self._shell_hosts():
            menu.addSection("SSH hosts")
            self._host_actions(menu, lambda a: self.split_with(pane, ConnectionSpec.ssh(a), orientation))

    def _split_chooser(self, pane: PaneBase, orientation: Qt.Orientation, pos: QPoint) -> None:
        menu = QMenu(self)
        self._fill_split_menu(menu, pane, orientation)
        menu.exec(pos)

    def split_choose(self, orientation: Qt.Orientation) -> None:
        """Keyboard/menu split: pick what opens next to the current terminal."""
        pane = self.tabs.current_item()
        if pane is None:
            self.open_local()
            return
        target = pane.focus_target()
        self._split_chooser(pane, orientation, target.mapToGlobal(target.rect().center()))

    def split_with(self, pane: PaneBase, spec: ConnectionSpec, orientation: Qt.Orientation) -> TerminalPane:
        page = self.tabs._page_of(pane)
        if page is not None:
            self.tabs.setCurrentWidget(page)
            page.active_pane = pane
        return self.open_spec(spec, split=orientation)

    # -- files (SFTP) ------------------------------------------------------ #
    def _file_hosts(self) -> list[str]:
        return [h.alias for h in self._shell_hosts()]

    def _make_file_pane(self, spec: ConnectionSpec | None) -> FilePane:
        return FilePane(spec, self.connections.open_file_system, self._file_hosts)

    def split_files_with(self, pane: PaneBase, spec: ConnectionSpec | None, orientation: Qt.Orientation) -> FilePane:
        page = self.tabs._page_of(pane)
        if page is not None:
            self.tabs.setCurrentWidget(page)
        new = self._make_file_pane(spec)
        self.tabs.split_pane(pane, new, orientation)
        return new

    def open_files(self, spec: ConnectionSpec | None, where: str = "tab", ref: PaneBase | None = None) -> FilePane:
        """Files instead of a terminal. ``where``: "tab" (this computer | host, like Termius), "right", "down"."""
        ref = ref or self.tabs.current_item()
        if where in ("right", "down") and ref is not None:
            orientation = Qt.Orientation.Vertical if where == "down" else Qt.Orientation.Horizontal
            return self.split_files_with(ref, spec, orientation)
        left = self._make_file_pane(ConnectionSpec.local())
        page = self.tabs.add_pane_tab(left)
        self.stack.setCurrentWidget(self.tabs)
        if spec is not None and spec.kind is ConnectionKind.LOCAL:
            spec = None  # right side: pick a host
        right = self._make_file_pane(spec)
        self.tabs.split_pane(left, right, Qt.Orientation.Horizontal)
        page.custom_title = f"SFTP · {spec.title}" if spec is not None else "SFTP"
        self.tabs._update_tab(page)
        return right

    def files_alias(self, alias: str, where: str) -> None:
        self.open_files(ConnectionSpec.ssh(alias), where)

    def split_alias(self, alias: str, where: str) -> None:
        """Sidebar "Open to the Right / Below": host next to the current terminal."""
        pane = self.tabs.current_pane()
        if pane is None:
            self.connect_alias(alias, new_tab=True)
            return
        orientation = Qt.Orientation.Vertical if where == "down" else Qt.Orientation.Horizontal
        self.split_with(pane, ConnectionSpec.ssh(alias), orientation)

    def toggle_broadcast(self) -> None:
        page = self.tabs.current_page()
        if page is None:
            self._broadcast_changed(False)
            return
        on = self.tabs.toggle_broadcast(page)
        if on and len(page.panes()) < 2:
            self.statusBar().showMessage("Broadcast is on — split this tab to type in several terminals at once", 6000)
        QTimer.singleShot(0, page.active_item.focus_target().setFocus)

    def _broadcast_changed(self, on: bool) -> None:
        self._actions["broadcast"].setChecked(on)
        self.broadcast_button.setChecked(on)
        color = current_palette().warning if on else current_palette().muted
        self.broadcast_button.setIcon(icon("broadcast", color))

    def arrange_grid(self) -> None:
        page = self.tabs.current_page()
        if page is not None:
            page.arrange_grid()
            QTimer.singleShot(0, page.active_item.focus_target().setFocus)

    def open_split_view(self, aliases: list[str]) -> None:
        """Open several hosts in one tab, laid out as a grid (like a split view)."""
        aliases = [a for a in aliases if a]
        if not aliases:
            return
        first = self.open_spec(ConnectionSpec.ssh(aliases[0]))
        page = self.tabs.current_page()
        for alias in aliases[1:]:
            pane = self._make_pane(ConnectionSpec.ssh(alias))
            self.tabs.split_pane(first, pane, Qt.Orientation.Horizontal)
            QTimer.singleShot(0, pane.start)
        if page is not None:
            page.custom_title = "Split view"
            page.arrange_grid()
            self.tabs.refresh_icons()

    # -- appearance ------------------------------------------------------- #
    def toggle_appearance(self) -> None:
        visible = not self.appearance.isVisible()
        if visible:
            pane = self.tabs.current_pane()
            scheme = (pane.terminal.scheme_override if pane else None) or self.settings.color_scheme
            self.appearance.load(scheme, self.settings.font_family, self.settings.font_size)
        self.appearance.setVisible(visible)
        self.theme_button.setChecked(visible)
        if visible:
            sizes = self.splitter.sizes()
            if len(sizes) == 3 and sizes[2] < 200:
                panel = 290
                self.splitter.setSizes([sizes[0], max(300, sizes[1] - panel), panel])

    def _scheme_chosen(self, name: str, scope: str) -> None:
        pane = self.tabs.current_pane()
        if scope == "pane" and pane is not None:
            pane.set_theme(name)
            return
        self.settings.color_scheme = name
        self.save_settings()
        for p in self.tabs.all_panes():
            p.apply_settings(self.settings)

    def _font_changed(self, family: str, size: int) -> None:
        if not family:
            return
        self.settings.font_family, self.settings.font_size = family, size
        self.save_settings()
        for p in self.tabs.all_panes():
            p.apply_settings(self.settings)

    def _split_from(self, pane: TerminalPane, orientation: Qt.Orientation) -> None:
        page = self.tabs.current_page()
        if page is not None:
            page.active_pane = pane
        self.split(orientation)

    def open_port_forwarding(self, pane: TerminalPane | None = None) -> None:
        pane = pane or self.tabs.current_pane()
        backend = pane.session.backend if pane else None
        if not isinstance(backend, SSHBackend) or backend.forwarding is None or pane is None:
            QMessageBox.information(self, "Port Forwarding", "Select a connected SSH tab first.")
            return
        PortForwardDialog(pane.title, backend.forwarding, self).exec()

    def test_connection(self, alias: str) -> None:
        try:
            resolved = self.ctx.ssh.resolve(alias)
        except (OSError, ConfigError) as exc:
            show_error(self, describe_exception(exc))
            return
        TestConnectionDialog(self.ctx.ssh, resolved, self.prompter, None, self).exec()

    def test_current_connection(self) -> None:
        pane = self.tabs.current_pane()
        alias = pane.spec.alias if pane and pane.spec.kind is ConnectionKind.SSH else self.sidebar.selected_alias()
        if alias:
            self.test_connection(alias)
        else:
            QMessageBox.information(self, "Test Connection", "Select a host in the sidebar first.")

    # ------------------------------------------------------------------ #
    # Teams
    # ------------------------------------------------------------------ #
    def _team_of_alias(self, alias: str) -> TeamInfo | None:
        host = self.ctx.config.get_host(alias)
        return self.ctx.teams.team_for_path(host.source_file) if host else None

    def _update_account_ui(self) -> None:
        teams = self.ctx.teams
        signed = teams.signed_in
        for key in ("sign_in", "create_account"):
            self._actions[key].setVisible(not signed)
        for key in ("teams", "sync_teams", "sign_out"):
            self._actions[key].setVisible(signed)
        if signed and teams.account:
            count = len(teams.teams)
            self.status_account.setText(f"👤 {teams.account.email} · {count} team{'s' if count != 1 else ''}")
            self.status_account.setToolTip(f"Team server: {teams.account.server}")
            minutes = max(1, self.settings.team_sync_minutes)
            self._team_timer.start(minutes * 60_000)
        else:
            self.status_account.setText("")
            self._team_timer.stop()

    def sign_in(self, create: bool = False) -> None:
        dlg = AccountDialog(self.ctx.teams, self.settings.team_server_url, create, self)
        if dlg.exec():
            self.settings.team_server_url = self.ctx.teams.account.server if self.ctx.teams.account else self.settings.team_server_url
            self.save_settings()
            self._update_account_ui()
            self.statusBar().showMessage(f"Signed in as {self.ctx.teams.account.email}", 5000)
            self.sync_teams(force=True)
            if create:
                self.open_teams()

    def sign_out(self) -> None:
        if not confirm(self, "Sign out", "Sign out? Team hosts will be removed from this computer until you sign in again.", "Sign Out"):
            return
        self.ctx.teams.logout()
        self.reload_config()
        self._update_account_ui()
        self.statusBar().showMessage("Signed out", 4000)

    def open_teams(self) -> None:
        if not self.ctx.teams.signed_in:
            self.sign_in()
            return
        TeamsDialog(self.ctx.teams, lambda: self.sync_teams(force=True), self).exec()
        self._update_account_ui()

    def sync_teams(self, force: bool = False, quiet: bool = False) -> None:
        teams = self.ctx.teams
        if not teams.signed_in or self._team_task is not None:
            return
        if not self.ctx.config.exists:
            self.ctx.config.create_config_file()
        path, writer = self.ctx.config.path, self.ctx.config.writer

        def done(result: SyncResult) -> None:
            self._team_task = None
            if not result.unchanged:
                self.reload_config()
            self._update_account_ui()
            message = f"Team hosts synced: {result.hosts_written} host(s) from {len(result.teams)} team(s)"
            if result.conflicts:
                message += f" — skipped (alias already in your config): {', '.join(result.conflicts)}"
            if result.rejected:
                message += f" — rejected as unsafe: {', '.join(result.rejected)}"
            if not (quiet and result.unchanged):
                self.statusBar().showMessage(message, 8000)

        def failed(exc: BaseException) -> None:
            self._team_task = None
            if isinstance(exc, TeamApiError) and exc.status == 401:
                teams.logout()
                self.reload_config()
                self._update_account_ui()
                show_error(self, FriendlyError("Session expired", "Your team session expired or was revoked. Sign in again."))
            elif not quiet:
                show_error(self, FriendlyError("Team sync failed", str(exc)))
            else:
                log.warning("Team sync failed: %s", exc)

        self._team_task = run_async(lambda: teams.sync(path, writer, force=force), on_done=done, on_error=failed, name="team-sync")

    def _host_keys_for(self, alias: str) -> list[str]:
        """Host key(s) already trusted locally, to publish with a shared host."""
        try:
            resolved = self.ctx.ssh.resolve(alias)
        except (OSError, ConfigError):
            return []
        store = KnownHostsStore([expand_user_path(p) for p in resolved.user_known_hosts_files])
        found = store.host_keys.lookup(host_id_for(resolved.host_key_alias or resolved.hostname, resolved.port)) or {}
        return [f"{key.get_name()} {key.get_base64()}" for key in found.values()]

    def share_host(self, alias: str, team_id: int) -> None:
        host = self.ctx.config.get_host(alias)
        teams = [t for t in self.ctx.teams.teams if t.can_edit]
        if host is None or not teams:
            return
        keys = self._host_keys_for(alias)
        groups = [*self.settings.groups, *self.ctx.teams.host_groups.values()]
        dlg = ShareHostDialog(alias, teams, bool(keys), self, groups=groups,
                              default_group=self.settings.group_of(alias) or "")
        dlg.team.setCurrentIndex(max(0, dlg.team.findData(team_id)))
        if not dlg.exec():
            return
        chosen = dlg.team.currentData()
        group = " ".join(dlg.group.text().split())
        payload = host_to_payload(host, group, keys)

        def shared(_r: object) -> None:
            where = f" in group “{group}”" if group else ""
            self.statusBar().showMessage(f"“{alias}” shared with the team{where}. Members will see it on their next sync.", 6000)
            self.sync_teams(force=True)

        def upsert() -> object:  # sharing again moves the host to the chosen group instead of failing
            existing = {h["alias"]: h["id"] for h in self.ctx.teams.team_hosts(chosen)}
            return self.ctx.teams.save_host(chosen, payload, existing.get(alias))

        run_async(upsert, on_done=shared,
                  on_error=lambda e: show_error(self, FriendlyError("Could not share host", str(e))), name="team-share")

    def share_group(self, group: str, team_id: int) -> None:
        """Share every personal host of a sidebar group with a team, keeping the group name."""
        team = next((t for t in self.ctx.teams.teams if t.id == team_id and t.can_edit), None)
        if team is None:
            return
        hosts = [h for a in self.settings.groups.get(group, [])
                 if (h := self.ctx.config.get_host(a)) is not None and self._team_of_alias(a) is None]
        if not hosts:
            self.statusBar().showMessage(f"“{group}” has no personal hosts to share.", 6000)
            return
        names = ", ".join(h.alias for h in hosts[:6]) + ("…" if len(hosts) > 6 else "")
        if not confirm(self, "Share group with team",
                       f"Share {len(hosts)} host(s) of “{group}” with team “{team.name}”?\n\n{names}\n\n"
                       f"Members will see them under “{team.name} › {group}”. Hosts that already exist in the team "
                       "are updated. Passwords and private keys are never shared.", ok_text="Share"):
            return
        payloads = [host_to_payload(h, group, self._host_keys_for(h.alias)) for h in hosts]

        def upload() -> tuple[int, int, list[str]]:
            existing = {h["alias"]: h["id"] for h in self.ctx.teams.team_hosts(team.id)}
            created = updated = 0
            errors: list[str] = []
            for payload in payloads:
                try:
                    host_id = existing.get(payload["alias"])
                    self.ctx.teams.save_host(team.id, payload, host_id)
                    if host_id:
                        updated += 1
                    else:
                        created += 1
                except Exception as exc:  # noqa: BLE001 - reported per host
                    errors.append(f"{payload['alias']}: {exc}")
            return created, updated, errors

        def done(result: tuple[int, int, list[str]]) -> None:
            created, updated, errors = result
            self.statusBar().showMessage(
                f"“{group}” shared with “{team.name}”: {created} added, {updated} updated.", 8000)
            if errors:
                show_error(self, FriendlyError("Some hosts were not shared", "\n".join(errors[:10])))
            self.sync_teams(force=True)

        self.statusBar().showMessage(f"Sharing “{group}” with “{team.name}”…")
        run_async(upload, on_done=done, name="team-share-group",
                  on_error=lambda e: show_error(self, FriendlyError("Could not share group", str(e))))

    def _team_host_record(self, team: TeamInfo, alias: str) -> dict | None:
        return next((h for h in self.ctx.teams.team_hosts(team.id) if h["alias"] == alias), None)

    def edit_team_host(self, alias: str) -> None:
        team = self._team_of_alias(alias)
        host = self.ctx.config.get_host(alias)
        if team is None or host is None:
            return
        if not team.can_edit:
            QMessageBox.information(self, "Team host", f"“{alias}” is managed by team “{team.name}”. Ask an admin to change it.")
            return
        dlg = ConnectionDialog(host, self._existing_aliases(), [], parent=self)
        dlg.setWindowTitle(f"Edit team host — {team.name}")
        if not dlg.exec():
            return
        edited = dlg.host

        def save() -> None:
            record = self._team_host_record(team, alias)
            if record is None:
                raise TeamApiError(f"“{alias}” no longer exists in team {team.name}")
            payload = host_to_payload(edited, record.get("group", ""), record.get("host_keys", []))
            self.ctx.teams.save_host(team.id, payload, record["id"])

        run_async(save, on_done=lambda _r: self.sync_teams(force=True),
                  on_error=lambda e: show_error(self, FriendlyError("Could not update team host", str(e))), name="team-edit")

    def delete_team_host(self, alias: str) -> None:
        team = self._team_of_alias(alias)
        if team is None or not team.can_edit:
            return
        if not confirm(self, "Remove from team", f"Remove “{alias}” from team “{team.name}” for everyone?", "Remove", danger=True):
            return

        def remove() -> None:
            record = self._team_host_record(team, alias)
            if record is not None:
                self.ctx.teams.delete_host(team.id, record["id"])

        run_async(remove, on_done=lambda _r: self.sync_teams(force=True),
                  on_error=lambda e: show_error(self, FriendlyError("Could not remove team host", str(e))), name="team-delete")

    # ------------------------------------------------------------------ #
    # Help
    # ------------------------------------------------------------------ #
    def show_diagnostics(self) -> None:
        rows = diagnostics_service.collect(self.ctx.config.path, len(self.ctx.config.hosts()), self.ctx.credentials.backend_name())
        DiagnosticsDialog(rows, QApplication.activeModalWidget() or self).exec()

    def show_about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(f"About {__app_name__}")
        box.setIconPixmap(app_icon().pixmap(64, 64))
        box.setText(about_text())
        box.exec()

    def show_documentation(self) -> None:
        keys = self.settings.keybindings
        rows = "".join(
            f"<tr><td style='padding-right:18px'>{name}</td><td><code>{keys.get(k, '')}</code></td></tr>"
            for k, name in (
                ("new_tab", "New local terminal tab"), ("close_tab", "Close tab / pane"), ("reopen_tab", "Reopen closed tab"),
                ("next_tab", "Next tab"), ("prev_tab", "Previous tab"), ("search", "Search connections"),
                ("copy", "Copy"), ("paste", "Paste"), ("split_right", "Split right"), ("split_down", "Split down"),
                ("settings", "Settings"),
            )
        )
        QMessageBox.information(
            self,
            "Documentation",
            "<h3>Quick reference</h3>"
            "<p>Hosts come from your SSH config. Double-click a host to connect in a new tab; right-click for more.</p>"
            f"<table>{rows}</table>"
            "<p>Shift+PageUp/PageDown scroll the terminal history; middle click pastes the selection (Linux).</p>"
            "<p>See README.md in the project for full documentation.</p>",
        )

    # ------------------------------------------------------------------ #
    # Startup / shutdown
    # ------------------------------------------------------------------ #
    def _after_show(self) -> None:
        self._update_account_ui()
        if self.ctx.teams.signed_in:
            self.sync_teams(quiet=True)
        s = self.settings
        if s.first_run:
            s.first_run = False
            self.save_settings()
            cfg = self.ctx.config
            dlg = WelcomeDialog(len(cfg.hosts()), cfg.exists, str(cfg.path), self)
            result = dlg.exec()
            if result == WelcomeDialog.CREATE:
                self._create_config()
            elif result == WelcomeDialog.IMPORT:
                self.reload_config(show_message=True)
        specs = list(self.startup.connect)
        if not specs and s.restore_tabs:
            specs = [ConnectionSpec.from_dict(d) for d in s.open_tabs if isinstance(d, dict)]
        for spec in specs:
            if spec.kind is ConnectionKind.SSH and spec.alias and self.ctx.config.get_host(spec.alias) is None and not spec.adhoc:
                show_error(self, FriendlyError("Unknown host", f"“{spec.alias}” is not defined in {self.ctx.config.path}."))
                continue
            self.open_spec(spec)

    def _restore_geometry(self) -> None:
        s = self.settings
        if s.window_geometry:
            try:
                self.restoreGeometry(QByteArray(base64.b64decode(s.window_geometry)))
            except (ValueError, TypeError) as exc:
                log.debug("Invalid stored geometry: %s", exc)
        width = max(200, s.sidebar_width) if s.sidebar_visible else 0
        self.splitter.setSizes([width, max(400, self.width() - width), 0])

    def _remember_sidebar_width(self) -> None:
        sizes = self.splitter.sizes()
        if sizes and sizes[0] > 0:
            self.settings.sidebar_width = sizes[0]

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        panes = self.tabs.all_panes()
        active = [p for p in panes if p.spec.kind is ConnectionKind.SSH and p.state is SessionState.CONNECTED]
        if active and self.settings.confirm_close_connected:
            names = ", ".join(sorted({p.title for p in active}))
            if not confirm(self, "Quit", f"{len(active)} active connection(s) will be closed:\n{names}\n\nQuit anyway?", "Quit", danger=True):
                event.ignore()
                return
        s = self.settings
        s.window_geometry = base64.b64encode(bytes(self.saveGeometry().data())).decode()
        self._remember_sidebar_width()
        s.open_tabs = [p.spec.to_dict() for p in panes] if s.restore_tabs else []
        self.save_settings()
        for pane in panes:
            pane.close_session()
        event.accept()

