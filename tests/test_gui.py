"""Offscreen GUI smoke tests (QT_QPA_PLATFORM=offscreen, set in conftest)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from ssh_terminal.models.connection import ConnectionSpec, SessionState  # noqa: E402
from ssh_terminal.ui.host_picker import ALIAS_ROLE  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def process(app, ms: int = 50) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class OfflineBackend:
    """Stays in 'connecting' and never touches the network."""

    kind = "ssh"
    user_closed = False
    encoding = "UTF-8"

    def __init__(self, title: str) -> None:
        self.description = f"offline:{title}"

    def set_callbacks(self, on_data, on_state, on_closed) -> None:
        self.on_state = on_state

    def start(self, cols: int, rows: int) -> None:
        self.on_state(SessionState.CONNECTING, "offline test backend")

    def write(self, data: bytes) -> None:
        pass

    def resize(self, cols: int, rows: int) -> None:
        pass

    def close(self) -> None:
        self.user_closed = True


@pytest.fixture
def window(qapp, ssh_config: Path):
    from ssh_terminal import __version__
    from ssh_terminal.services.app_context import AppContext
    from ssh_terminal.ui.main_window import MainWindow

    ctx = AppContext.create()
    ctx.settings.first_run = False
    ctx.settings.last_seen_version = __version__  # no "What's New" popup in GUI tests
    ctx.resolver.prefer_openssh = False
    w = MainWindow(ctx)
    # No real network in GUI tests: SSH panes get an idle offline backend
    # (real SSH is covered by tests/integration against a live sshd).
    w.connections._ssh_backend = lambda spec: OfflineBackend(spec.title)
    w.show()
    yield w
    for pane in w.tabs.all_panes():
        pane.close_session()
    w.ctx.settings.confirm_close_connected = False
    w.close()


def test_main_window_lists_hosts(window) -> None:
    sidebar = window.sidebar
    aliases = set()
    for i in range(sidebar.tree.topLevelItemCount()):
        sec = sidebar.tree.topLevelItem(i)
        for j in range(sec.childCount()):
            aliases.add(sec.child(j).text(0))
    assert {"server-prod", "server-hml", "bastion", "database", "server", "*"} <= aliases


def test_search_filters(window) -> None:
    window.sidebar.search.setText("hml")
    visible = []
    tree = window.sidebar.tree
    for i in range(tree.topLevelItemCount()):
        sec = tree.topLevelItem(i)
        for j in range(sec.childCount()):
            if not sec.child(j).isHidden():
                visible.append(sec.child(j).text(0))
    assert visible == ["server-hml"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell")
def test_local_terminal_tab_split_close_reopen(window, qapp) -> None:
    window.open_spec(ConnectionSpec.local(""))
    for _ in range(40):
        process(qapp)
        pane = window.tabs.current_pane()
        if pane and pane.state is SessionState.CONNECTED:
            break
    assert window.tabs.count() == 1
    assert window.tabs.current_pane().state is SessionState.CONNECTED
    window.split(Qt.Orientation.Horizontal)
    process(qapp, 200)
    assert len(window.tabs.all_panes()) == 2
    window.close_current()
    assert len(window.tabs.all_panes()) == 1
    window.close_current()
    assert window.tabs.count() == 0
    window.reopen_tab()
    process(qapp, 200)
    assert window.tabs.count() == 1


def test_connection_dialog_collect(qapp, ssh_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from ssh_terminal.models.ssh_host import SSHHost
    from ssh_terminal.ui import connection_dialog
    from ssh_terminal.ui.connection_dialog import ConnectionDialog

    warnings: list[str] = []

    class StubBox:
        StandardButton = QMessageBox.StandardButton

        @staticmethod
        def warning(_parent, _title, text):
            warnings.append(text)

        @staticmethod
        def question(*_args):
            return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(connection_dialog, "QMessageBox", StubBox)

    original = SSHHost(alias="srv", hostname="10.0.0.1", user="lucas", port=22, identity_files=["~/.ssh/id"],
                       extra_options=[("CustomOption", "something")])
    dlg = ConnectionDialog(original, {"srv", "other"}, [])
    dlg.port_edit.setText("2222")
    host = dlg._collect()
    assert host is not None and host.port == 2222
    assert host.extra_options == [("CustomOption", "something")]
    dlg.alias_edit.setText("other")
    assert dlg._collect() is None
    assert warnings and "already exists" in warnings[-1]


def test_terminal_widget_input(qapp) -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    from ssh_terminal.models.app_settings import AppSettings
    from ssh_terminal.terminal.terminal_widget import TerminalWidget

    w = TerminalWidget(AppSettings())
    w.resize(400, 200)
    sent: list[bytes] = []
    w.input_ready.connect(sent.append)
    # the physical Control key: Qt calls it Meta on macOS (Control there is ⌘)
    control = Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin" else Qt.KeyboardModifier.ControlModifier
    QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, control, "\x03"))
    QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier, "\t"))
    assert sent == [b"\x03", b"\t"]
    w.feed(b"hello\r\nworld")
    process(qapp)
    w.select_all()
    assert w.selected_text().startswith("hello\nworld")
    w.feed(b"\x1b[?2004h")
    process(qapp)
    sent.clear()
    w.send_text("a\nb", paste=True)
    assert sent == [b"\x1b[200~a\rb\x1b[201~"]


def test_git_hosts_get_their_own_section(qapp, isolated_home: Path) -> None:
    from ssh_terminal.models.app_settings import AppSettings
    from ssh_terminal.ssh.config_parser import SSHConfigDocument
    from ssh_terminal.ui.connection_panel import KIND_GIT, KIND_HOST, ROLE_KIND, ConnectionPanel
    from test_host_classifier import CONFIG

    panel = ConnectionPanel()
    hosts = SSHConfigDocument.parse(CONFIG).hosts()
    settings = AppSettings()
    panel.populate(hosts, [], settings)
    sections = {}
    for i in range(panel.tree.topLevelItemCount()):
        sec = panel.tree.topLevelItem(i)
        sections[sec.text(0)] = [(sec.child(j).text(0), sec.child(j).data(0, ROLE_KIND)) for j in range(sec.childCount())]
    assert sections["HOSTS"] == [("painel", KIND_HOST)]
    assert sections["GIT & SERVICES"] == [("lucas.cardoso", KIND_GIT), ("github-pessoal", KIND_GIT)]

    tested, connected = [], []
    panel.git_test_requested.connect(tested.append)
    panel.connect_requested.connect(lambda a, _n: connected.append(a))
    git_item = panel.tree.topLevelItem(1).child(0)
    panel.tree.itemDoubleClicked.emit(git_item, 0)
    assert tested == ["lucas.cardoso"] and connected == []

    settings.host_roles["github-pessoal"] = "server"  # manual override
    panel.populate(hosts, [], settings)
    assert [panel.tree.topLevelItem(0).child(j).text(0) for j in range(panel.tree.topLevelItem(0).childCount())] == ["painel", "github-pessoal"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell")
def test_split_view_grid_and_pane_themes(window, qapp) -> None:
    window.open_split_view(["server-prod", "server-hml", "bastion", "database"])
    process(qapp, 200)
    page = window.tabs.current_page()
    panes = page.panes()
    assert len(panes) == 4 and window.tabs.tabText(window.tabs.indexOf(page)).startswith("Workstation 1")
    # 2x2 grid: a vertical splitter holding two horizontal rows
    root = page._root()
    assert root.orientation() == Qt.Orientation.Vertical and root.count() == 2
    assert all(root.widget(i).count() == 2 for i in range(2))

    panes[0].set_theme("Kanagawa Wave")
    assert panes[0].terminal.scheme.name == "Kanagawa Wave" and panes[0].spec.theme == "Kanagawa Wave"
    window._scheme_chosen("Hacker Green", "all")
    assert panes[1].terminal.scheme.name == "Hacker Green"
    assert panes[0].terminal.scheme.name == "Kanagawa Wave"  # per-terminal override wins
    panes[0].set_theme(None)
    assert panes[0].terminal.scheme.name == "Hacker Green"

    window._pane_action(panes[3], "close")
    assert len(page.panes()) == 3
    window.toggle_appearance()
    assert window.appearance.isVisible()


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell")
def test_split_with_other_host_and_broadcast_input(window, qapp) -> None:
    first = window.open_spec(ConnectionSpec.local(""))
    process(qapp, 100)
    # a *different* terminal side by side (SSH host from the config)
    other = window.split_with(first, ConnectionSpec.ssh("server-hml"), Qt.Orientation.Horizontal)
    window.split_alias("bastion", "down")
    page = window.tabs.current_page()
    panes = page.panes()
    assert len(panes) == 3 and {p.spec.title for p in panes} >= {"server-hml", "bastion"}
    assert other.spec.alias == "server-hml"

    # broadcast: typing in one pane is written to the others
    written = {id(p): [] for p in panes}
    for p in panes:
        p.session.write = lambda data, p=p: written[id(p)].append(data)
        p.session.state = SessionState.CONNECTED
    panes[0].terminal.user_input.emit(b"ls\r")
    assert written[id(panes[1])] == []  # off by default
    window.toggle_broadcast()
    assert page.broadcast and all(p.broadcast for p in panes)
    assert window._actions["broadcast"].isChecked()
    panes[0].terminal.user_input.emit(b"uptime\r")
    assert written[id(panes[1])] == [b"uptime\r"] and written[id(panes[2])] == [b"uptime\r"]
    assert written[id(panes[0])] == []  # the source writes through input_ready itself
    assert window.tabs.tabText(window.tabs.currentIndex()).startswith("⦿")
    window._pane_action(panes[1], "broadcast")
    assert not page.broadcast and not window._actions["broadcast"].isChecked()


def test_split_menu_lists_hosts_and_shells(window, qapp) -> None:
    from PySide6.QtWidgets import QMenu

    pane = window.open_spec(ConnectionSpec.local(""))
    menu = QMenu()
    window._fill_split_menu(menu, pane, Qt.Orientation.Horizontal)
    try:
        texts = [a.text() for a in menu.actions()]
        assert texts[0].startswith("Same Connection")
        picker = _pickers(menu)[-1]  # "SSH hosts" (the first one is inside "Files (SFTP)")
        assert "server-prod" in [i.data(0, ALIAS_ROLE) for i in picker.hosts()]
    finally:
        pane.close_session()


def test_nested_splits_keep_every_pane(window, qapp) -> None:
    """Right, then down inside the right one, then close: no pane gets lost."""
    first = window.open_spec(ConnectionSpec.local(""))
    right = window.split_with(first, ConnectionSpec.ssh("server-hml"), Qt.Orientation.Horizontal)
    below = window.split_with(right, ConnectionSpec.ssh("bastion"), Qt.Orientation.Vertical)
    page = window.tabs.current_page()
    assert len(page.panes()) == 3
    root = page._root()
    assert root.orientation() == Qt.Orientation.Horizontal and root.count() == 2
    assert root.widget(1).orientation() == Qt.Orientation.Vertical
    window.tabs.close_pane(below)
    assert set(page.panes()) == {first, right}
    assert page._root().count() == 2
    for p in page.panes():
        p.close_session()


def _wait_for(qapp, predicate, tries: int = 60) -> None:
    for _ in range(tries):
        if predicate():
            return
        process(qapp, 50)


def test_sidebar_toggle_keeps_grip_to_pull_it_back(window, qapp) -> None:
    from ssh_terminal.ui.grip_splitter import GripHandle

    process(qapp, 50)
    assert window._sidebar_open() and window.sidebar_button.isChecked()
    window.sidebar_button.click()
    assert window.splitter.sizes()[0] == 0 and not window.settings.sidebar_visible
    handle = window.splitter.handle(1)
    assert isinstance(handle, GripHandle) and handle.isVisible() and handle.width() >= 6  # still there to pull
    window.splitter.handle_double_clicked.emit(1)  # double-click the grip
    assert window._sidebar_open() and window.sidebar_button.isChecked()
    # dragging the grip to the edge collapses it, and the state follows
    window.splitter.moveSplitter(0, 1)
    window._sidebar_moved()
    assert not window.settings.sidebar_visible and not window._actions["toggle_sidebar"].isChecked()
    window.splitter.moveSplitter(260, 1)
    window._sidebar_moved()
    assert window.settings.sidebar_visible and window.settings.sidebar_width >= 200


def test_share_group_with_team_and_team_subgroups(window, qapp, monkeypatch) -> None:
    import ssh_terminal.ui.main_window as mw
    from ssh_terminal.services.team_service import TeamInfo

    team = TeamInfo(7, "devops", "devops", "owner")
    saved: list[tuple[int, dict, int | None]] = []
    teams = window.ctx.teams
    monkeypatch.setattr(type(teams), "teams", property(lambda self: [team]), raising=False)
    monkeypatch.setattr(teams, "team_hosts", lambda team_id: [{"alias": "server-hml", "id": 41, "user": "deploy"}])
    monkeypatch.setattr(teams, "save_host", lambda tid, payload, host_id=None: saved.append((tid, payload, host_id)) or {})
    monkeypatch.setattr(window, "sync_teams", lambda *a, **k: None)
    monkeypatch.setattr(mw, "confirm", lambda *a, **k: True)
    window.settings.groups = {"Staging EU": ["server-prod", "server-hml"]}
    window.share_group("Staging EU", 7)
    _wait_for(qapp, lambda: len(saved) == 2)
    by_alias = {p["alias"]: (tid, p, hid) for tid, p, hid in saved}
    assert by_alias["server-prod"][2] is None and by_alias["server-hml"][2] == 41  # create vs update
    assert all(p["group"] == "Staging EU" and tid == 7 for tid, p, _ in saved)
    # a new host goes up without my login; an existing one keeps the login the team already shares
    assert by_alias["server-prod"][1]["user"] == "" and by_alias["server-hml"][1]["user"] == "deploy"

    # the group section offers "Share Group with Team"
    window.sidebar.populate(window.ctx.config.hosts(), [], window.settings, editable_teams=[team])
    tree = window.sidebar.tree
    sections = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    assert any(sec.text(0) == "STAGING EU" for sec in sections)


def test_team_hosts_show_in_team_subgroups(qapp, ssh_config: Path) -> None:
    from ssh_terminal.models.app_settings import AppSettings
    from ssh_terminal.models.ssh_host import SSHHost
    from ssh_terminal.services.team_service import TeamInfo
    from ssh_terminal.ui.connection_panel import ROLE_SECTION, ConnectionPanel

    team = TeamInfo(7, "devops", "devops", "member")
    hosts = [SSHHost(alias="vm-a", hostname="1.1.1.1"), SSHHost(alias="vm-b", hostname="1.1.1.2", comment="web box"),
             SSHHost(alias="vm-c", hostname="1.1.1.3")]
    groups = {"vm-a": "Staging EU", "vm-b": "Staging EU / Web"}
    panel = ConnectionPanel()
    panel.populate(hosts, [], AppSettings(), team_of=lambda h: team, team_group_of=lambda a: groups.get(a, ""))
    tree = panel.tree
    root = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).text(0) == "TEAM · DEVOPS")
    # sub-groups first, then the team's ungrouped hosts
    assert [root.child(j).text(0) for j in range(root.childCount())] == ["STAGING EU", "vm-c"]
    staging = root.child(0)
    assert [staging.child(j).text(0) for j in range(staging.childCount())] == ["WEB", "vm-a"]
    web = staging.child(0)
    assert web.data(0, ROLE_SECTION) == "team:devops:Staging EU/Web"
    vm_b = web.child(0)
    assert vm_b.text(0) == "vm-b"
    assert "web box" in vm_b.toolTip(0) and "devops" in vm_b.toolTip(0)
    assert "asked on first access" in vm_b.toolTip(0)  # shared without a login
    panel.search.setText("vm-b")
    assert not web.isHidden() and staging.child(1).isHidden() and root.child(1).isHidden()
    assert panel._first_visible_host() == "vm-b"


def test_personal_subgroups(qapp, ssh_config: Path) -> None:
    from ssh_terminal.models.app_settings import AppSettings
    from ssh_terminal.models.ssh_host import SSHHost
    from ssh_terminal.ui.connection_panel import ConnectionPanel

    settings = AppSettings()
    settings.set_group("web-1", "Production / Web")
    settings.set_group("db-1", "Production")
    settings.add_group("Production/Empty")
    hosts = [SSHHost(alias=a, hostname=f"10.0.0.{i}") for i, a in enumerate(["web-1", "db-1", "loose"], 1)]
    panel = ConnectionPanel()
    panel.populate(hosts, [], settings)
    tree = panel.tree
    prod = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).text(0) == "PRODUCTION")
    assert [prod.child(j).text(0) for j in range(prod.childCount())] == ["EMPTY", "WEB", "db-1"]
    assert prod.child(1).child(0).text(0) == "web-1"
    assert panel._host_count(prod) == 2


def test_files_tab_drop_upload_and_mixed_split(window, qapp, tmp_path: Path) -> None:
    from PySide6.QtCore import QMimeData, QUrl

    from ssh_terminal.ui.file_pane import FilePane

    right = window.open_files(None)  # new tab: this computer | host picker
    page = window.tabs.current_page()
    left = [i for i in page.items() if i is not right][0]
    assert isinstance(left, FilePane) and right.stack.currentIndex() == 2  # picker
    picker_labels = [right.picker.item(i).text() for i in range(right.picker.count())]
    assert picker_labels[0] == "This computer" and "server-prod" in picker_labels

    dest = tmp_path / "dest"
    dest.mkdir()
    right.connect_to(ConnectionSpec.local())
    _wait_for(qapp, lambda: right.fs is not None and right.cwd)
    right.navigate(str(dest))
    _wait_for(qapp, lambda: right.cwd == str(dest))

    # drop a file from the OS (Explorer) onto the panel -> copied/uploaded into the folder
    src = tmp_path / "report.txt"
    src.write_text("dados")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(src))])
    assert right.list._acceptable(mime)
    right._dropped({"local_paths": [str(src)]}, right.cwd)
    _wait_for(qapp, lambda: (dest / "report.txt").exists() and right._running is None and not right._transfers)
    assert (dest / "report.txt").read_text() == "dados"
    _wait_for(qapp, lambda: any(e.name == "report.txt" for e in right._entries))

    # drag between panels (custom mime payload) and move into a folder within the same panel
    (dest / "sub").mkdir()
    right.refresh()
    _wait_for(qapp, lambda: any(e.name == "sub" for e in right._entries))
    items = [right.list.topLevelItem(i) for i in range(right.list.topLevelItemCount())]
    report_item = [it for it in items if it.text(0) == "report.txt"][0]
    import json
    payload = json.loads(bytes(right.list.mimeData([report_item]).data("application/x-veylix-files")).decode())
    right._dropped(payload, str(dest / "sub"))
    _wait_for(qapp, lambda: (dest / "sub" / "report.txt").exists())
    assert not (dest / "report.txt").exists()

    # a terminal and a file pane share one tab; closing works for both kinds
    term = window.split_with(right, ConnectionSpec.local(""), Qt.Orientation.Vertical)
    assert len(page.items()) == 3 and page.panes() == [term]
    window._pane_action(right, "close")
    assert right not in page.items() and len(page.items()) == 2
    window.tabs.close_pane(term)
    assert page.items() == [left] and page.active_item is left
    window.tabs.close_pane(left)
    assert window.tabs.count() == 0


def test_split_menu_offers_files(window, qapp) -> None:
    from PySide6.QtWidgets import QMenu

    pane = window.open_spec(ConnectionSpec.local(""))
    menu = QMenu()
    window._fill_split_menu(menu, pane, Qt.Orientation.Horizontal)
    files = [a for a in menu.actions() if a.text() == "Files (SFTP)"][0].menu()
    labels = [a.text() for a in files.actions()]
    hosts = [i.data(0, ALIAS_ROLE) for i in _pickers(files)[0].hosts()]
    assert labels[0] == "This Computer" and "server-hml" in hosts
    files.actions()[0].trigger()
    from ssh_terminal.ui.file_pane import FilePane

    page = window.tabs.current_page()
    assert any(isinstance(i, FilePane) for i in page.items())
    for i in page.items():
        i.close_session()


def test_worker_callbacks_after_session_destroyed_do_not_crash(qapp) -> None:
    """A backend thread keeps reporting while its tab is closed (crashed on Windows CI)."""
    import threading

    from ssh_terminal.terminal.backends.base import CloseInfo
    from ssh_terminal.terminal.terminal_session import TerminalSession, _sessions

    class ChattyBackend:
        kind = "fake"
        user_closed = False

        def set_callbacks(self, on_data, on_state, on_closed):
            self.cb = (on_data, on_state, on_closed)

        def start(self, cols, rows):
            self.stop = threading.Event()

            def run():
                while not self.stop.wait(0.001):
                    self.cb[0](b"x")
                    self.cb[1](SessionState.CONNECTING, "still trying")
                self.cb[2](CloseInfo("bye"))

            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()

        def close(self):
            pass

    backends = []
    for _ in range(20):
        backend = ChattyBackend()
        backends.append(backend)
        session = TerminalSession(ConnectionSpec.local(""), lambda b=backend: b)
        session.start(80, 24)
        process(qapp, 5)
        key = session._key
        session.deleteLater()
        process(qapp, 5)
        assert key not in _sessions
    process(qapp, 50)
    for backend in backends:
        backend.stop.set()
        backend.thread.join(2)
    process(qapp, 50)  # queued events for dead sessions are dropped silently


def test_share_single_host_keeps_or_picks_group(window, qapp, monkeypatch) -> None:
    import ssh_terminal.ui.main_window as mw
    from ssh_terminal.services.team_service import TeamInfo

    team = TeamInfo(7, "devops", "devops", "owner")
    saved: list[tuple[dict, int | None]] = []
    teams = window.ctx.teams
    monkeypatch.setattr(type(teams), "teams", property(lambda self: [team]), raising=False)
    monkeypatch.setattr(teams, "team_hosts", lambda team_id: [{"alias": "server-hml", "id": 41}])
    monkeypatch.setattr(teams, "save_host", lambda tid, payload, host_id=None: saved.append((payload, host_id)) or {})
    monkeypatch.setattr(window, "sync_teams", lambda *a, **k: None)
    teams.host_groups = {"other-vm": "PROD ASIA"}
    window.settings.groups = {"Staging EU": ["server-prod", "server-hml"]}
    seen = {}

    class FakeDialog(mw.ShareHostDialog):
        def exec(self):
            seen["groups"] = [self.group_combo.itemText(i) for i in range(self.group_combo.count())]
            seen["default"] = self.group.text()
            return True

    monkeypatch.setattr(mw, "ShareHostDialog", FakeDialog)
    window.share_host("server-prod", 7)
    _wait_for(qapp, lambda: len(saved) == 1)
    assert seen["default"] == "Staging EU" and {"Staging EU", "PROD ASIA"} <= set(seen["groups"])
    assert saved[0][0]["group"] == "Staging EU" and saved[0][1] is None
    window.share_host("server-hml", 7)  # already in the team -> updated (moved into the group)
    _wait_for(qapp, lambda: len(saved) == 2)
    assert saved[1][0]["group"] == "Staging EU" and saved[1][1] == 41


def test_whats_new_shows_once_per_version(qapp, ssh_config: Path) -> None:
    from ssh_terminal import __version__
    from ssh_terminal.services.app_context import AppContext
    from ssh_terminal.ui.info_dialogs import WhatsNewDialog
    from ssh_terminal.ui.main_window import MainWindow

    def launch(last_seen: str):
        ctx = AppContext.create()
        ctx.settings.first_run = False
        ctx.settings.last_seen_version = last_seen
        ctx.resolver.prefer_openssh = False
        w = MainWindow(ctx)
        w.connections._ssh_backend = lambda spec: OfflineBackend(spec.title)
        w._after_show()
        process(qapp, 600)
        dialogs = [d for d in qapp.topLevelWidgets() if isinstance(d, WhatsNewDialog) and d.isVisible()]
        return w, dialogs

    w, dialogs = launch("1.0.0")  # just updated
    assert len(dialogs) == 1 and w.settings.last_seen_version == __version__
    text = dialogs[0].notes.toPlainText()
    assert "Broadcast input" in text and "First release" not in text  # only what changed since 1.0.0
    assert "Thanks for updating" in dialogs[0].windowTitle() or any(
        "Thanks for updating" in lbl.text() for lbl in dialogs[0].findChildren(QLabel))
    dialogs[0].accept()
    w.close()

    w2, dialogs2 = launch(__version__)  # opened again: nothing
    assert dialogs2 == []
    w2.show_whats_new(all_releases=True)  # Help › What's New shows everything
    process(qapp, 50)
    assert "First release" in w2._whats_new.notes.toPlainText()
    w2._whats_new.accept()
    w2.close()


def test_new_or_edited_host_cannot_duplicate_an_existing_one(window, qapp) -> None:
    from ssh_terminal.models.ssh_host import SSHHost

    host = window.ctx.config.get_host  # sample config: server-prod = lucas@10.0.0.10:22, database via bastion
    # same server as server-prod (user unset counts as the same login)
    msg = window._duplicate_problem(SSHHost(alias="prod-copy", hostname="10.0.0.10"))
    assert msg and "already saved as" in msg and "server-prod" in msg
    # same name, other case
    assert "already exists" in window._duplicate_problem(SSHHost(alias="Server-HML", hostname="9.9.9.9"))
    # a different server is fine; so is 10.0.0.20 without the bastion (another network)
    assert window._duplicate_problem(SSHHost(alias="new", hostname="10.0.0.99")) is None
    assert window._duplicate_problem(SSHHost(alias="db-direct", hostname="10.0.0.20", user="postgres")) is None
    # editing a host without changing its server never complains (also after Duplicate)
    original = host("server-hml")
    edited = SSHHost(alias="server-hml", hostname=original.hostname, user=original.user, port=original.port,
                     identity_files=["~/.ssh/other"])
    assert window._duplicate_problem(edited, original) is None
    # ...but moving it onto another host's server does
    moved = SSHHost(alias="server-hml", hostname="10.0.0.20", user="postgres", proxy_jump="bastion")
    assert "database" in window._duplicate_problem(moved, original)


def test_sidebar_never_lists_a_host_twice(window, qapp, isolated_home: Path) -> None:
    cfg = isolated_home / ".ssh" / "config"
    cfg.write_text(cfg.read_text() + "\nHost server-hml\n    HostName 6.6.6.6\n")  # repeated block
    window.reload_config()
    tree = window.sidebar.tree
    names = [tree.topLevelItem(i).child(j).text(0) for i in range(tree.topLevelItemCount())
             for j in range(tree.topLevelItem(i).childCount())
             if tree.topLevelItem(i).text(0) == "HOSTS"]
    assert names.count("server-hml") == 1


def test_delete_key_on_team_host_removes_it_from_my_list(window, qapp, monkeypatch) -> None:
    import ssh_terminal.ui.main_window as mw
    from ssh_terminal.services.team_service import TeamInfo

    member = TeamInfo(3, "devops", "devops", "member")
    hidden: list[str] = []
    monkeypatch.setattr(window, "_team_of_alias", lambda alias: member if alias == "server-hml" else None)
    monkeypatch.setattr(window.ctx.teams, "hide_host", lambda team, alias, writer=None: hidden.append(f"{team.slug}/{alias}"))
    monkeypatch.setattr(mw, "confirm", lambda *a, **k: True)
    removed_from_config: list[str] = []
    monkeypatch.setattr(window.ctx.config, "remove_host", lambda alias: removed_from_config.append(alias))
    window.delete_connection("server-hml")  # member: never touches the team or the personal config
    assert hidden == ["devops/server-hml"] and removed_from_config == []


def test_team_host_asks_for_my_own_login_first(window, monkeypatch) -> None:
    import ssh_terminal.ui.main_window as mw
    from ssh_terminal.models.ssh_host import SSHHost
    from ssh_terminal.services.team_service import TeamInfo

    team = TeamInfo(7, "devops", "devops", "member")
    host = SSHHost(alias="team-box", hostname="10.1.1.1")
    monkeypatch.setattr(window, "_team_of_alias", lambda a: team if a == "team-box" else None)
    real_get = window.ctx.config.get_host
    monkeypatch.setattr(window.ctx.config, "get_host", lambda a: host if a == "team-box" else real_get(a))
    monkeypatch.setattr(window, "reload_config", lambda *a, **k: None)
    asked: list[tuple[str, str, str]] = []
    answer = {"ok": False}

    class _Text:
        def __init__(self, value: str) -> None:
            self.value = value

        def text(self) -> str:
            return self.value

    class FakeDialog:
        def __init__(self, alias, team_name, address, user, identity, can_save, parent) -> None:  # noqa: ANN001
            asked.append((alias, team_name, address))
            self.user, self.identity, self.password = _Text("ana"), _Text(""), _Text("")

        def exec(self) -> bool:
            return answer["ok"]

    monkeypatch.setattr(mw, "TeamLoginDialog", FakeDialog)
    saved: list[tuple[str, str, str]] = []
    teams = window.ctx.teams
    monkeypatch.setattr(teams, "set_my_login", lambda t, a, u, i="": saved.append((a, u, i)))
    before = window.tabs.count()
    window.connect_alias("team-box")  # cancelled: nothing opens
    assert asked == [("team-box", "devops", "10.1.1.1")] and window.tabs.count() == before and not saved
    answer["ok"] = True
    assert window.ensure_team_login("team-box") and saved == [("team-box", "ana", "")]
    # once a login exists (or the host carries a user) nobody is asked again
    monkeypatch.setattr(teams, "my_login", lambda slug, alias: {"user": "ana", "identity_file": ""})
    assert window.ensure_team_login("team-box") and len(asked) == 2
    assert window.ensure_team_login("server-prod")  # personal host: never asked


def test_local_terminal_offers_every_shell(window) -> None:
    from PySide6.QtWidgets import QMenu

    from ssh_terminal.utils.platform import ShellProfile

    window.connections._shells = [ShellProfile("bash", ["/bin/bash"]), ShellProfile("zsh", ["/bin/zsh"])]
    window.settings.default_local_shell = "zsh"
    menu = QMenu(window)
    window._fill_shell_menu(menu)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts[0].startswith("zsh") and "(default)" in texts[0] and texts[1] == "bash"
    assert texts[-1] == "Default Shell"
    window.set_default_shell("bash")
    assert window.settings.default_local_shell == "bash"
    assert window.connections.shell_profile().name == "bash"


def test_share_host_never_touches_widgets_from_the_worker(window, qapp, monkeypatch) -> None:
    """Regression: reading the dialog's checkbox inside the worker crashed the app (SIGSEGV)."""
    import threading

    import ssh_terminal.ui.main_window as mw
    from ssh_terminal.services.team_service import TeamInfo

    team = TeamInfo(7, "devops", "devops", "owner")
    teams = window.ctx.teams
    monkeypatch.setattr(type(teams), "teams", property(lambda self: [team]), raising=False)
    monkeypatch.setattr(teams, "team_hosts", lambda team_id: [{"alias": "server-prod", "id": 5, "user": "deploy"}])
    saved: list[tuple[dict, int | None]] = []
    monkeypatch.setattr(teams, "save_host", lambda tid, payload, host_id=None: saved.append((payload, host_id)) or {})
    monkeypatch.setattr(window, "sync_teams", lambda *a, **k: None)
    checked_from: list[threading.Thread] = []

    class _Box:
        def isChecked(self) -> bool:  # noqa: N802 - Qt name
            checked_from.append(threading.current_thread())
            return False

    class _Combo:
        def setCurrentIndex(self, _i: int) -> None:  # noqa: N802
            pass

        def findData(self, _d: int) -> int:  # noqa: N802
            return 0

        def currentData(self) -> int:  # noqa: N802
            return 7

    class _Line:
        def text(self) -> str:
            return " Prod / Web "

    class FakeShare:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            self.team, self.group, self.share_login = _Combo(), _Line(), _Box()

        def exec(self) -> bool:
            return True

    monkeypatch.setattr(mw, "ShareHostDialog", FakeShare)
    window.share_host("server-prod", 7)
    _wait_for(qapp, lambda: "shared with the team" in window.statusBar().currentMessage())
    assert len(saved) == 1
    assert checked_from and all(t is threading.main_thread() for t in checked_from)
    payload, host_id = saved[0]
    assert host_id == 5 and payload["group"] == "Prod/Web" and payload["user"] == "deploy"


def test_drag_and_dock_moves_panes(window, qapp) -> None:
    from PySide6.QtCore import QPoint, QRect

    from ssh_terminal.ui.dock import Zone, zone_at, zone_rect

    rect = QRect(0, 0, 200, 100)
    assert zone_at(rect, QPoint(10, 50)) is Zone.LEFT and zone_at(rect, QPoint(190, 50)) is Zone.RIGHT
    assert zone_at(rect, QPoint(100, 5)) is Zone.TOP and zone_at(rect, QPoint(100, 95)) is Zone.BOTTOM
    assert zone_rect(rect, Zone.RIGHT) == QRect(100, 0, 100, 100)
    assert Zone.TOP.before and not Zone.BOTTOM.before and Zone.LEFT.orientation is Qt.Orientation.Horizontal

    a = window.open_spec(ConnectionSpec.ssh("server-prod"))
    b = window.split_with(a, ConnectionSpec.ssh("server-hml"), Qt.Orientation.Horizontal)
    page = window.tabs.current_page()
    process(qapp, 50)
    # drag b above a: the tab becomes a vertical stack [b, a]
    window.tabs.move_pane(b, a, Qt.Orientation.Vertical, True)
    root = page._root()
    assert root.orientation() == Qt.Orientation.Vertical and [root.widget(i) for i in range(root.count())] == [b, a]
    # a third one docked left of a: nested horizontal [c, a] under b
    c = window.dock_host("bastion", a, Qt.Orientation.Horizontal, True)
    process(qapp, 50)
    assert c is not None and set(page.items()) == {a, b, c}
    inner = page._root().widget(1)
    assert inner.orientation() == Qt.Orientation.Horizontal and [inner.widget(i) for i in range(inner.count())] == [c, a]
    # move c back under everything: the emptied splitter collapses, nothing is lost
    window.tabs.move_pane(c, b, Qt.Orientation.Vertical, False)
    root = page._root()
    assert [root.widget(i) for i in range(root.count())] == [b, c, a] and set(page.items()) == {a, b, c}
    assert page.active_item is c and all(p.isVisible() for p in (a, b, c))
    window.tabs.move_pane(c, c, Qt.Orientation.Vertical, False)  # onto itself: no change
    assert set(page.items()) == {a, b, c}

    # the overlay finds the pane and edge under the cursor, never the dragged pane itself
    overlay = window.tabs._overlay(page)
    overlay.begin(a)
    process(qapp, 30)
    b_rect = QRect(b.mapTo(page, QPoint(0, 0)), b.size())
    assert overlay.locate(QPoint(b_rect.left() + 3, b_rect.center().y())) == (b, Zone.LEFT)
    a_rect = QRect(a.mapTo(page, QPoint(0, 0)), a.size())
    assert overlay.locate(a_rect.center()) == (None, None)
    overlay.end()
    assert not overlay.isVisible()

    # the real drop path: pane drop moves it, host drop opens the host there
    from PySide6.QtCore import QMimeData

    from ssh_terminal.ui.dock import HOST_MIME, PANE_MIME

    overlay.begin(c)
    mime = QMimeData()
    mime.setData(PANE_MIME, b"c")
    overlay._on_drop(mime, a, Zone.RIGHT)
    overlay.end()
    process(qapp, 50)
    parent = a.parentWidget()
    assert parent.orientation() == Qt.Orientation.Horizontal and parent.indexOf(c) == parent.indexOf(a) + 1
    overlay.begin(None)
    host = QMimeData()
    host.setData(HOST_MIME, b"database")
    overlay._on_drop(host, b, Zone.TOP)
    overlay.end()
    process(qapp, 80)
    new = next(p for p in page.panes() if p.spec.alias == "database")
    assert new.parentWidget().indexOf(new) == new.parentWidget().indexOf(b) - 1
    for p in page.panes():
        p.close_session()


def test_sidebar_host_drag_carries_alias(window) -> None:
    from ssh_terminal.ui.connection_panel import KIND_HOST, ROLE_ALIAS, ROLE_KIND
    from ssh_terminal.ui.dock import HOST_MIME

    tree = window.sidebar.tree
    hosts = []

    def walk(item) -> None:  # noqa: ANN001
        for j in range(item.childCount()):
            child = item.child(j)
            if child.data(0, ROLE_KIND) == KIND_HOST:
                hosts.append(child)
            walk(child)

    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    mime = tree.mimeData([hosts[0]])
    assert bytes(mime.data(HOST_MIME)).decode() == hosts[0].data(0, ROLE_ALIAS)
    assert not tree.mimeData([tree.topLevelItem(0)]).hasFormat(HOST_MIME)  # sections are not hosts


def _flush() -> None:
    for _ in range(5):
        QApplication.processEvents()


def _pickers(menu):  # noqa: ANN001, ANN202
    from PySide6.QtWidgets import QWidgetAction

    from ssh_terminal.ui.host_picker import HostPicker

    found = []
    for action in menu.actions():
        if isinstance(action, QWidgetAction) and isinstance(action.defaultWidget(), HostPicker):
            found.append(action.defaultWidget())
        elif action.menu() is not None:
            found.extend(_pickers(action.menu()))
    return found


def _tree_labels(item):  # noqa: ANN001, ANN202
    return [item.child(i).text(0) for i in range(item.childCount())]


def test_split_menu_groups_hosts_like_the_sidebar(window) -> None:
    from PySide6.QtWidgets import QMenu

    window.settings.groups = {"Prod": ["server-prod"], "Prod/Web": ["server-hml"]}
    window.settings.favorites = ["bastion"]
    menu = QMenu()
    picked = []
    window.ensure_team_login = lambda alias: True
    window._host_actions(menu, picked.append)
    picker = _pickers(menu)[0]
    root = picker.tree.invisibleRootItem()
    top = _tree_labels(root)
    assert top[0].startswith("bastion")  # favorites first
    prod = next(root.child(i) for i in range(root.childCount()) if root.child(i).text(0) == "Prod")
    inside = _tree_labels(prod)
    assert inside[0] == "Web" and any(t.startswith("server-prod") for t in inside)
    assert [t.split()[0] for t in _tree_labels(prod.child(0))] == ["server-hml"]
    assert not any(t.startswith(("server-prod", "server-hml")) for t in top)  # not repeated at the top
    assert sum(i.data(0, ALIAS_ROLE) == "bastion" for i in picker.hosts()) == 1  # a favorite is listed once
    # the search keeps a group's path visible and hides the rest
    picker.search.setText("hml")
    assert [i.data(0, ALIAS_ROLE) for i in picker.hosts(visible_only=True)] == ["server-hml"]
    assert not prod.isHidden() and prod.isExpanded()
    picker.search.setText("web")  # a matching group shows its hosts
    assert [i.data(0, ALIAS_ROLE) for i in picker.hosts(visible_only=True)] == ["server-hml"]
    # clicking a group folds it; clicking a host picks it (after the menu closes)
    picker.search.setText("")
    expanded = prod.isExpanded()
    picker._clicked(prod, 0)
    assert prod.isExpanded() is not expanded and picked == []
    picker._clicked(prod.child(0).child(0), 0)
    _flush()
    assert picked == ["server-hml"]


def test_pane_can_be_docked_beside_a_pane_of_another_tab(window, qapp) -> None:
    a = window.open_spec(ConnectionSpec.local(""))
    page_a = window.tabs.current_page()
    b = window.open_spec(ConnectionSpec.local(""))  # a second tab
    page_b = window.tabs.current_page()
    _flush()
    assert page_a is not page_b and window.tabs.count() == 2
    try:
        window.tabs.move_pane(b, a, Qt.Orientation.Horizontal, False)
        assert window.tabs.count() == 1 and window.tabs.current_page() is page_a  # the emptied tab is gone
        assert page_a.items() == [a, b] or set(page_a.items()) == {a, b}
        root = page_a._root()
        assert root.orientation() == Qt.Orientation.Horizontal and root.widget(1) is b
        # signals follow the pane to its new tab
        a.activated.emit(a)
        assert page_a.active_item is a
        b.activated.emit(b)
        assert page_a.active_item is b and window.tabs.current_item() is b
    finally:
        _flush()
        for p in page_a.items():
            p.close_session()


def test_hovering_a_tab_title_while_dragging_switches_tab(window, qapp) -> None:
    a = window.open_spec(ConnectionSpec.local(""))
    page_a = window.tabs.current_page()
    b = window.open_spec(ConnectionSpec.local(""))
    _flush()
    try:
        window.tabs._dragging = b
        window.tabs._hover_tab(window.tabs.indexOf(page_a))
        assert window.tabs.current_page() is page_a
        assert page_a.overlay is not None and page_a.overlay.source is b and not page_a.overlay.isHidden()
    finally:
        window.tabs._dragging = None
        window.tabs.end_host_drag()
        _flush()
        a.close_session()
        b.close_session()


def test_tool_buttons_open_beside_the_terminal(window, qapp, monkeypatch) -> None:
    from ssh_terminal.ui.docker_pane import DockerPane
    from ssh_terminal.ui.sql_pane import SqlPane

    def no_ssh(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("offline in tests")

    monkeypatch.setattr(window.connections, "open_ssh", no_ssh)
    window.ensure_team_login = lambda alias: True
    pane = window.open_spec(ConnectionSpec.ssh("server-hml"))
    page = window.tabs.current_page()
    _flush()
    try:
        window._pane_action(pane, "docker")
        docker = next(i for i in page.items() if isinstance(i, DockerPane))
        assert window.tabs.count() == 1 and page._root().widget(1) is docker  # to the right, same tab
        # Docker → "Open in SQL console": the console opens beside the Docker card, the database preselected
        from ssh_terminal.services.docker_service import DatabaseCandidate

        cand = DatabaseCandidate("db", "postgres", 5432, "postgres:16", "172.17.0.4", "app", "shop", "pw")
        docker.sql_requested.emit(docker.spec, cand)
        sql = next(i for i in page.items() if isinstance(i, SqlPane))
        assert sql.preset is cand and window.tabs.count() == 1
        _flush()
        # the split menu offers the tools too
        from PySide6.QtWidgets import QMenu

        menu = QMenu()
        window._fill_split_menu(menu, pane, Qt.Orientation.Vertical)
        assert any(a.text() == "Docker on server-hml" for a in menu.actions())
    finally:
        for p in page.items():
            p.close_session()


def test_sql_dialog_prefills_a_docker_database(qapp) -> None:
    from ssh_terminal.services.docker_service import DatabaseCandidate
    from ssh_terminal.ui.sql_pane import SqlConnectDialog

    cand = DatabaseCandidate("db", "postgres", 5432, "postgres:16", "172.17.0.4", "app", "shop", "pw")
    other = DatabaseCandidate("my", "mysql", 3307, "mariadb:11")
    dlg = SqlConnectDialog("box", [], [other, cand], lambda _p: None, preferred=cand)
    assert dlg.saved.currentData() is cand
    assert (dlg.host.text(), dlg.port.value(), dlg.user.text(), dlg.database.text(), dlg.password.text()) == \
        ("172.17.0.4", 5432, "app", "shop", "pw")
    assert not dlg.remember.isChecked()
    dlg.deleteLater()


def test_pulling_a_tab_down_docks_its_panel_next_to_the_previous_tab(window, qapp, monkeypatch) -> None:
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    import ssh_terminal.ui.terminal_tabs as tt

    window.open_spec(ConnectionSpec.local(""))
    page_a = window.tabs.current_page()
    b = window.open_spec(ConnectionSpec.local(""))  # its own tab, now the current one
    page_b = window.tabs.current_page()
    _flush()
    seen = {}

    class FakeDrag:  # QDrag.exec blocks on a real desktop: record what the user would see instead
        def __init__(self, source) -> None:  # noqa: ANN001
            seen["source"] = source

        def setMimeData(self, mime) -> None:  # noqa: ANN001, N802
            seen["mime"] = mime

        def setPixmap(self, _pixmap) -> None:  # noqa: ANN001, N802
            pass

        def exec(self, _action) -> None:  # noqa: ANN001
            seen["page"] = window.tabs.current_page()
            seen["overlay"] = page_a.overlay is not None and page_a.overlay.source is b

    monkeypatch.setattr(tt, "QDrag", FakeDrag)
    bar = window.tabs.tabBar()
    try:
        center = QPointF(bar.tabRect(window.tabs.indexOf(page_b)).center())
        below = QPointF(center.x(), bar.height() + 40)
        left, none = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        qapp.sendEvent(bar, QMouseEvent(QEvent.Type.MouseButtonPress, center, bar.mapToGlobal(center), left, left, none))
        qapp.sendEvent(bar, QMouseEvent(QEvent.Type.MouseMove, below, bar.mapToGlobal(below), Qt.MouseButton.NoButton, left, none))
        assert seen["source"] is b and seen["page"] is page_a and seen["overlay"]  # the tab it came from, with drop zones
        # dropping on a tab title puts the panel to the right of that tab's panel
        window.tabs._dragging = b
        window.tabs._drop_on_tab(window.tabs.indexOf(page_a), seen["mime"])
        window.tabs._dragging = None
        _flush()
        assert window.tabs.count() == 1 and page_a._root().widget(1) is b
    finally:
        _flush()
        for p in page_a.items():
            p.close_session()


def test_tool_cards_can_be_made_narrow(qapp, tmp_path) -> None:
    from ssh_terminal.ui.docker_pane import DockerPane
    from ssh_terminal.ui.sql_pane import SqlPane

    def no_ssh(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("offline")

    for card in (DockerPane(ConnectionSpec.ssh("box"), no_ssh), SqlPane(ConnectionSpec.ssh("box"), no_ssh, app_dir=tmp_path)):
        card.show_content()
        header = card.layout().itemAt(0).widget().minimumSizeHint().width()
        # only the title bar sets the minimum (font sizes differ per OS); the content scrolls instead
        assert card.minimumSizeHint().width() <= header + 10 < card.content.minimumSizeHint().width()
        card.close_session()
        card.deleteLater()
    _flush()


def test_select_copies_and_right_click_pastes(qapp) -> None:
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QGuiApplication, QMouseEvent

    from ssh_terminal.models.app_settings import AppSettings
    from ssh_terminal.terminal.terminal_widget import TerminalWidget

    w = TerminalWidget(AppSettings())
    w.resize(400, 200)
    w.show()
    sent: list[bytes] = []
    w.input_ready.connect(sent.append)
    w.feed(b"hello world")
    process(qapp)
    cw, chh = w.cell_size
    vp = w.viewport()
    left, right, none = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier

    def mouse(kind, x, button, buttons, mods=none):  # noqa: ANN001, ANN202
        pos = QPointF(x, chh / 2)
        QApplication.sendEvent(vp, QMouseEvent(kind, pos, vp.mapToGlobal(pos), button, buttons, mods))

    QGuiApplication.clipboard().setText("")
    mouse(QEvent.Type.MouseButtonPress, cw * 0.2, left, left)
    mouse(QEvent.Type.MouseMove, cw * 4.6, Qt.MouseButton.NoButton, left)
    mouse(QEvent.Type.MouseButtonRelease, cw * 4.6, left, Qt.MouseButton.NoButton)
    assert QGuiApplication.clipboard().text() == "hello"  # selecting copies
    mouse(QEvent.Type.MouseButtonPress, cw * 8, right, right)
    assert sent == [b"hello"] and not w.has_selection()  # right click pastes
    menus = []
    w.context_menu_requested.connect(menus.append)
    mouse(QEvent.Type.MouseButtonPress, cw * 8, right, right, Qt.KeyboardModifier.ShiftModifier)
    assert menus and sent == [b"hello"]  # Shift+right click: the menu, no paste
    w.deleteLater()


def test_tabs_with_several_panels_are_numbered_workstations(window, qapp) -> None:
    tabs = window.tabs
    a = window.open_spec(ConnectionSpec.local(""))
    page_a = tabs.current_page()
    window.split_with(a, ConnectionSpec.local(""), Qt.Orientation.Horizontal)
    window.split_with(a, ConnectionSpec.local(""), Qt.Orientation.Vertical)
    c = window.open_spec(ConnectionSpec.local(""))
    page_c = tabs.current_page()
    single = tabs.tabText(tabs.indexOf(page_c))
    window.split_with(c, ConnectionSpec.local(""), Qt.Orientation.Horizontal)
    _flush()
    try:
        assert tabs.tabText(tabs.indexOf(page_a)) == "Workstation 1  [3]"
        assert tabs.tabText(tabs.indexOf(page_c)) == "Workstation 2  [2]"
        assert "Workstation" not in single  # one host: the tab keeps the host's name
        tabs.close_pane(next(p for p in page_c.items() if p is not c))  # back to one panel
        assert tabs.tabText(tabs.indexOf(page_c)) == c.title and page_c.workstation == 0
        tabs.close_tab(tabs.indexOf(page_a))  # number 1 is free again
        window.split_with(c, ConnectionSpec.local(""), Qt.Orientation.Horizontal)
        assert tabs.tabText(tabs.indexOf(page_c)).startswith("Workstation 1")
    finally:
        _flush()
        for page in tabs.pages():
            for p in page.items():
                p.close_session()
