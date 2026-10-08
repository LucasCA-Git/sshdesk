"""Offscreen GUI smoke tests (QT_QPA_PLATFORM=offscreen, set in conftest)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ssh_terminal.models.connection import ConnectionSpec, SessionState  # noqa: E402


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
    from ssh_terminal.services.app_context import AppContext
    from ssh_terminal.ui.main_window import MainWindow

    ctx = AppContext.create()
    ctx.settings.first_run = False
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
    QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier, "\x03"))
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
    assert len(panes) == 4 and page.custom_title == "Split view"
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
    texts = [a.text() for a in menu.actions()]
    assert texts[0].startswith("Same Connection")
    assert any(t.startswith("server-prod") for t in texts)
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
    monkeypatch.setattr(teams, "team_hosts", lambda team_id: [{"alias": "server-hml", "id": 41}])
    monkeypatch.setattr(teams, "save_host", lambda tid, payload, host_id=None: saved.append((tid, payload, host_id)) or {})
    monkeypatch.setattr(window, "sync_teams", lambda *a, **k: None)
    monkeypatch.setattr(mw, "confirm", lambda *a, **k: True)
    window.settings.groups = {"Staging EU": ["server-prod", "server-hml"]}
    window.share_group("Staging EU", 7)
    _wait_for(qapp, lambda: len(saved) == 2)
    by_alias = {p["alias"]: (tid, p, hid) for tid, p, hid in saved}
    assert by_alias["server-prod"][2] is None and by_alias["server-hml"][2] == 41  # create vs update
    assert all(p["group"] == "Staging EU" and tid == 7 for tid, p, _ in saved)

    # the group section offers "Share Group with Team"
    window.sidebar.populate(window.ctx.config.hosts(), [], window.settings, editable_teams=[team])
    tree = window.sidebar.tree
    sections = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    assert any(sec.text(0) == "STAGING EU" for sec in sections)


def test_team_hosts_show_in_team_subgroups(qapp, ssh_config: Path) -> None:
    from ssh_terminal.models.app_settings import AppSettings
    from ssh_terminal.models.ssh_host import SSHHost
    from ssh_terminal.services.team_service import TeamInfo
    from ssh_terminal.ui.connection_panel import ConnectionPanel

    team = TeamInfo(7, "devops", "devops", "member")
    hosts = [SSHHost(alias="vm-a", hostname="1.1.1.1"), SSHHost(alias="vm-b", hostname="1.1.1.2", comment="web box"),
             SSHHost(alias="vm-c", hostname="1.1.1.3")]
    groups = {"vm-a": "Staging EU", "vm-b": "Staging EU"}
    panel = ConnectionPanel()
    panel.populate(hosts, [], AppSettings(), team_of=lambda h: team, team_group_of=lambda a: groups.get(a, ""))
    tree = panel.tree
    sections = {tree.topLevelItem(i).text(0): [tree.topLevelItem(i).child(j).text(0) for j in range(tree.topLevelItem(i).childCount())]
                for i in range(tree.topLevelItemCount())}
    assert sections["TEAM · DEVOPS"] == ["vm-c"]
    assert sections["TEAM · DEVOPS › STAGING EU"] == ["vm-a", "vm-b"]
    vm_b = tree.topLevelItem(1).child(1)
    assert "web box" in vm_b.toolTip(0) and "devops" in vm_b.toolTip(0)


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
    payload = json.loads(bytes(right.list.mimeData([report_item]).data("application/x-sshdesk-files")).decode())
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
    assert labels[0] == "This Computer" and any(t.startswith("server-hml") for t in labels)
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
