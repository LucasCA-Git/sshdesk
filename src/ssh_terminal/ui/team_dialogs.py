"""Account (sign in / create account) and team management dialogs.

All network calls go through :func:`run_async`, so the window never freezes
while the server answers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.services.team_service import TeamApiError, TeamInfo, TeamService
from ssh_terminal.ui.dialogs import confirm, mark_primary
from ssh_terminal.ui.workers import run_async

ROLE_LABELS = {"owner": "Owner", "admin": "Admin", "member": "Member"}


def _err(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, TeamApiError) else f"{type(exc).__name__}: {exc}"


class _Busy:
    """Disable a set of widgets while a request runs."""

    def __init__(self, *widgets: QWidget) -> None:
        self.widgets = widgets
        for w in widgets:
            w.setEnabled(False)
        QGuiApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)

    def done(self) -> None:
        for w in self.widgets:
            w.setEnabled(True)
        QGuiApplication.restoreOverrideCursor()


# ---------------------------------------------------------------------- #
class AccountDialog(QDialog):
    """Sign in or create an account on an SSHDesk Server."""

    def __init__(self, teams: TeamService, server: str, create: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.teams = teams
        self.setWindowTitle("SSHDesk Account")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        header = QLabel("Team sharing")
        header.setObjectName("DialogHeader")
        layout.addWidget(header)
        intro = QLabel("Sign in to an SSHDesk Server to see the hosts shared by your teams. "
                       "Only host definitions are shared — your keys and passwords never leave this computer.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        layout.addWidget(intro)

        server_row = QFormLayout()
        self.server = QLineEdit(server)
        self.server.setPlaceholderText("https://sshdesk.example.com")
        server_row.addRow("Server", self.server)
        layout.addLayout(server_row)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("DialogTabs")
        sign_in = QWidget()
        form = QFormLayout(sign_in)
        self.login_email = QLineEdit()
        self.login_email.setPlaceholderText("you@company.com")
        self.login_password = QLineEdit()
        self.login_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Email", self.login_email)
        form.addRow("Password", self.login_password)
        self.tabs.addTab(sign_in, "Sign In")

        create_tab = QWidget()
        form = QFormLayout(create_tab)
        self.reg_name = QLineEdit()
        self.reg_email = QLineEdit()
        self.reg_email.setPlaceholderText("use the email that received the invite")
        self.reg_password = QLineEdit()
        self.reg_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.reg_password.setPlaceholderText("at least 8 characters")
        self.reg_confirm = QLineEdit()
        self.reg_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Name", self.reg_name)
        form.addRow("Email", self.reg_email)
        form.addRow("Password", self.reg_password)
        form.addRow("Confirm", self.reg_confirm)
        self.tabs.addTab(create_tab, "Create Account")
        layout.addWidget(self.tabs)
        if create:
            self.tabs.setCurrentIndex(1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = mark_primary(QPushButton("Continue"))
        self.ok.setDefault(True)
        self.ok.clicked.connect(self._submit)
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok)
        layout.addLayout(buttons)
        (self.reg_name if create else self.login_email).setFocus()

    def _fail(self, message: str) -> None:
        self.status.setText(f"<span style='color:#f0605a'>{message}</span>")

    def _submit(self) -> None:
        server = self.server.text().strip().rstrip("/")
        if not server.startswith(("http://", "https://")):
            self._fail("Server must start with https:// (or http:// for local tests).")
            return
        if self.tabs.currentIndex() == 0:
            email, password = self.login_email.text().strip(), self.login_password.text()
            if not email or not password:
                self._fail("Enter email and password.")
                return
            job = lambda: self.teams.login(server, email, password)  # noqa: E731
        else:
            email, password = self.reg_email.text().strip(), self.reg_password.text()
            if len(password) < 8:
                self._fail("Password must have at least 8 characters.")
                return
            if password != self.reg_confirm.text():
                self._fail("Passwords do not match.")
                return
            name = self.reg_name.text().strip()
            job = lambda: self.teams.register(server, email, password, name)  # noqa: E731
        busy = _Busy(self.ok, self.tabs, self.server)
        self.status.setText("Contacting server…")

        def done(_account: Any) -> None:
            busy.done()
            self.accept()

        def failed(exc: BaseException) -> None:
            busy.done()
            self._fail(_err(exc))

        self._task = run_async(job, on_done=done, on_error=failed, name="team-auth")


# ---------------------------------------------------------------------- #
class TeamsDialog(QDialog):
    """Create teams, accept invites, invite people, manage members, browse shared hosts."""

    def __init__(self, teams: TeamService, on_changed: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.teams = teams
        self.on_changed = on_changed
        self._tasks: list[Any] = []
        self.setWindowTitle("Teams")
        self.resize(860, 560)
        root = QVBoxLayout(self)

        account = teams.account
        who = QLabel(f"Signed in as <b>{account.email}</b> on {account.server}" if account else "Not signed in")
        root.addWidget(who)

        self.invites_box = QLabel("")
        self.invites_box.setWordWrap(True)
        self.invites_box.hide()
        root.addWidget(self.invites_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.team_list = QListWidget()
        self.team_list.currentRowChanged.connect(self._team_selected)
        left_layout.addWidget(self.team_list, 1)
        row = QHBoxLayout()
        create = QPushButton("New Team")
        create.clicked.connect(self._create_team)
        accept = mark_primary(QPushButton("Accept Invite"))
        accept.clicked.connect(self._accept_invite)
        row.addWidget(create)
        row.addWidget(accept)
        left_layout.addLayout(row)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("")
        self.title.setObjectName("DialogHeader")
        right_layout.addWidget(self.title)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("DialogTabs")

        self.hosts_table = self._table(["Alias", "Address", "Group", "Updated by"])
        self.tabs.addTab(self.hosts_table, "Hosts")

        members_page = QWidget()
        mp = QVBoxLayout(members_page)
        self.members_table = self._table(["Email", "Name", "Role"])
        mp.addWidget(self.members_table)
        mrow = QHBoxLayout()
        self.role_combo = QComboBox()
        for role in ("member", "admin", "owner"):
            self.role_combo.addItem(ROLE_LABELS[role], role)
        self.set_role_btn = QPushButton("Change Role")
        self.set_role_btn.clicked.connect(self._change_role)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_member)
        mrow.addWidget(self.role_combo)
        mrow.addWidget(self.set_role_btn)
        mrow.addWidget(self.remove_btn)
        mrow.addStretch(1)
        mp.addLayout(mrow)
        self.tabs.addTab(members_page, "Members")

        invites_page = QWidget()
        ip = QVBoxLayout(invites_page)
        self.invites_table = self._table(["Email", "Role", "Expires", "Invited by"])
        ip.addWidget(self.invites_table)
        irow = QHBoxLayout()
        self.invite_btn = mark_primary(QPushButton("Invite by Email"))
        self.invite_btn.clicked.connect(self._invite)
        self.revoke_btn = QPushButton("Revoke")
        self.revoke_btn.clicked.connect(self._revoke)
        irow.addWidget(self.invite_btn)
        irow.addWidget(self.revoke_btn)
        irow.addStretch(1)
        ip.addLayout(irow)
        self.tabs.addTab(invites_page, "Invites")
        right_layout.addWidget(self.tabs, 1)

        team_row = QHBoxLayout()
        self.rename_btn = QPushButton("Rename Team")
        self.rename_btn.clicked.connect(self._rename)
        self.leave_btn = QPushButton("Leave Team")
        self.leave_btn.clicked.connect(self._leave)
        self.delete_btn = QPushButton("Delete Team")
        self.delete_btn.clicked.connect(self._delete_team)
        team_row.addWidget(self.rename_btn)
        team_row.addStretch(1)
        team_row.addWidget(self.leave_btn)
        team_row.addWidget(self.delete_btn)
        right_layout.addLayout(team_row)
        splitter.addWidget(right)
        splitter.setSizes([260, 600])
        root.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        root.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self._team_cache: list[TeamInfo] = []
        self.refresh()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        return table

    def _fill(self, table: QTableWidget, rows: list[list[str]], keys: list[Any]) -> None:
        table.setRowCount(len(rows))
        for r, (values, key) in enumerate(zip(rows, keys, strict=True)):
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, key)
                table.setItem(r, c, item)

    def _selected_key(self, table: QTableWidget) -> Any:
        row = table.currentRow()
        item = table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _call(self, func: Callable[[], Any], done: Callable[[Any], None], message: str = "") -> None:
        if message:
            self.status.setText(message)

        def ok(result: Any) -> None:
            self.status.setText("")
            done(result)

        def fail(exc: BaseException) -> None:
            self.status.setText(f"<span style='color:#f0605a'>{_err(exc)}</span>")

        self._tasks.append(run_async(func, on_done=ok, on_error=fail, name="teams"))

    @property
    def current(self) -> TeamInfo | None:
        row = self.team_list.currentRow()
        return self._team_cache[row] if 0 <= row < len(self._team_cache) else None

    # ------------------------------------------------------------------ #
    def refresh(self, select_id: int | None = None) -> None:
        def loaded(result: tuple[list[TeamInfo], list[dict]]) -> None:
            teams, invites = result
            current = select_id or (self.current.id if self.current else None)
            self._team_cache = teams
            self.team_list.clear()
            for team in teams:
                item = QListWidgetItem(f"{team.name}\n{ROLE_LABELS.get(team.role, team.role)} · {team.hosts} hosts · {team.members} members")
                self.team_list.addItem(item)
            index = next((i for i, t in enumerate(teams) if t.id == current), 0 if teams else -1)
            self.team_list.setCurrentRow(index)
            if not teams:
                self._team_selected(-1)
            if invites:
                names = ", ".join(f"<b>{i['team_name']}</b> (from {i['invited_by'] or 'an admin'})" for i in invites)
                self.invites_box.setText(f"You have pending invites: {names}. Click <b>Accept Invite</b> and enter the code you received.")
                self.invites_box.show()
            else:
                self.invites_box.hide()

        self._call(lambda: (self.teams.list_teams(), self.teams.my_invites()), loaded, "Loading teams…")

    def _team_selected(self, _row: int) -> None:
        team = self.current
        has = team is not None
        for w in (self.tabs, self.rename_btn, self.leave_btn, self.delete_btn):
            w.setEnabled(has)
        if not has:
            self.title.setText("No team yet — create one or accept an invite.")
            for table in (self.hosts_table, self.members_table, self.invites_table):
                table.setRowCount(0)
            return
        assert team is not None
        admin = team.can_edit
        self.title.setText(f"{team.name}  ·  you are {ROLE_LABELS.get(team.role, team.role)}")
        for w in (self.invite_btn, self.revoke_btn, self.set_role_btn, self.remove_btn, self.role_combo, self.rename_btn):
            w.setEnabled(admin)
        self.delete_btn.setEnabled(team.role == "owner")
        self.tabs.setTabEnabled(2, admin)

        def loaded(result: tuple[list[dict], list[dict], list[dict]]) -> None:
            hosts, members, invites = result
            self._fill(
                self.hosts_table,
                [[h["alias"], f"{h['user'] + '@' if h['user'] else ''}{h['hostname']}:{h['port']}", h["group"], h["updated_by"]] for h in hosts],
                [h["id"] for h in hosts],
            )
            self._fill(self.members_table, [[m["email"], m["name"], ROLE_LABELS.get(m["role"], m["role"])] for m in members],
                       [m["user_id"] for m in members])
            self._fill(self.invites_table, [[i["email"], i["role"], i["expires_at"][:10], i["invited_by"]] for i in invites],
                       [i["id"] for i in invites])

        def fetch() -> tuple[list[dict], list[dict], list[dict]]:
            hosts = self.teams.team_hosts(team.id)
            members = self.teams.members(team.id)
            invites = self.teams.team_invites(team.id) if admin else []
            return hosts, members, invites

        self._call(fetch, loaded, f"Loading {team.name}…")

    def _changed(self, select_id: int | None = None) -> None:
        self.refresh(select_id)
        self.on_changed()

    # -- actions ------------------------------------------------------------ #
    def _create_team(self) -> None:
        name, ok = QInputDialog.getText(self, "New team", "Team name:")
        if ok and name.strip():
            self._call(lambda: self.teams.create_team(name.strip()), lambda t: self._changed(t.id), "Creating team…")

    def _accept_invite(self) -> None:
        code, ok = QInputDialog.getText(self, "Accept invite", "Invite code (e.g. K7QH-M2XP-9TWA):")
        if ok and code.strip():
            def joined(team: TeamInfo) -> None:
                QMessageBox.information(self, "Welcome", f"You joined “{team.name}”. Its hosts will appear in the sidebar.")
                self._changed(team.id)

            self._call(lambda: self.teams.accept_invite(code.strip()), joined, "Joining team…")

    def _invite(self) -> None:
        team = self.current
        if team is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Invite to {team.name}")
        form = QFormLayout(dlg)
        email = QLineEdit()
        email.setPlaceholderText("person@company.com")
        role = QComboBox()
        role.addItem("Member (view hosts)", "member")
        role.addItem("Admin (edit hosts and invite)", "admin")
        form.addRow("Email", email)
        form.addRow("Role", role)
        send = mark_primary(QPushButton("Send Invite"))
        send.clicked.connect(dlg.accept)
        form.addRow("", send)
        if dlg.exec() != QDialog.DialogCode.Accepted or not email.text().strip():
            return
        address, chosen = email.text().strip(), role.currentData()

        def created(inv: dict[str, Any]) -> None:
            self._show_code(inv)
            self._team_selected(self.team_list.currentRow())

        self._call(lambda: self.teams.invite(team.id, address, chosen), created, "Creating invite…")

    def _show_code(self, inv: dict[str, Any]) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Invite created")
        how = ("An email with the code was sent." if inv.get("email_sent")
               else "The server has no email (SMTP) configured — send this code to the person yourself.")
        box.setText(
            f"Invite for <b>{inv['email']}</b> ({inv['role']}).<br><br>{how}<br><br>"
            f"Code: <span style='font-size:18px'><b><code>{inv['code']}</code></b></span><br><br>"
            "They sign in (or create an account) with that email and use <b>Teams › Accept Invite</b>."
        )
        copy = box.addButton("Copy Code", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is copy:
            QGuiApplication.clipboard().setText(inv["code"])

    def _revoke(self) -> None:
        team, invite_id = self.current, self._selected_key(self.invites_table)
        if team and invite_id:
            self._call(lambda: self.teams.revoke_invite(team.id, invite_id), lambda _r: self._team_selected(0), "Revoking…")

    def _change_role(self) -> None:
        team, user_id = self.current, self._selected_key(self.members_table)
        if team and user_id:
            role = self.role_combo.currentData()
            self._call(lambda: self.teams.set_role(team.id, user_id, role), lambda _r: self._team_selected(0), "Updating role…")

    def _remove_member(self) -> None:
        team, user_id = self.current, self._selected_key(self.members_table)
        if team and user_id and confirm(self, "Remove member", "Remove this person from the team?", "Remove", danger=True):
            self._call(lambda: self.teams.remove_member(team.id, user_id), lambda _r: self._changed(team.id), "Removing…")

    def _rename(self) -> None:
        team = self.current
        if team is None:
            return
        name, ok = QInputDialog.getText(self, "Rename team", "New name:", text=team.name)
        if ok and name.strip():
            self._call(lambda: self.teams.client().request("PATCH", f"/teams/{team.id}", {"name": name.strip()}),
                       lambda _r: self._changed(team.id), "Renaming…")

    def _leave(self) -> None:
        team = self.current
        if team and confirm(self, "Leave team", f"Leave “{team.name}”? Its hosts will disappear from your sidebar.", "Leave", danger=True):
            self._call(lambda: self.teams.leave(team.id), lambda _r: self._changed(), "Leaving…")

    def _delete_team(self) -> None:
        team = self.current
        if team and confirm(self, "Delete team", f"Delete “{team.name}” and all its shared hosts for everyone?", "Delete", danger=True):
            self._call(lambda: self.teams.delete_team(team.id), lambda _r: self._changed(), "Deleting…")


class ShareHostDialog(QDialog):
    """Pick the team (and group) to share a personal host with."""

    def __init__(self, alias: str, teams: list[TeamInfo], has_host_key: bool, parent: QWidget | None = None,
                 groups: list[str] | None = None, default_group: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Share “{alias}” with a team")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.team = QComboBox()
        for team in teams:
            self.team.addItem(team.name, team.id)
        # Group inside the team: pick an existing one (yours or the team's) or type a new one
        self.group_combo = QComboBox()
        self.group_combo.setEditable(True)
        self.group_combo.addItem("")
        for name in sorted({g for g in (groups or []) if g} | ({default_group} if default_group else set()), key=str.lower):
            self.group_combo.addItem(name)
        self.group_combo.setCurrentText(default_group)
        self.group_combo.lineEdit().setPlaceholderText("no group — or pick/type one, e.g. Staging EU")
        self.group = self.group_combo.lineEdit()
        form.addRow("Team", self.team)
        form.addRow("Group", self.group_combo)
        layout.addLayout(form)
        shared = ("Shared: HostName, User, Port, ProxyJump, IdentityFile path (as a hint), safe options"
                  + (" and the host key fingerprint from your known_hosts" if has_host_key else "") + ".")
        note = QLabel(shared + "\nNever shared: private keys, passwords, ProxyCommand or any command-running option.")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = mark_primary(QPushButton("Share"))
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
