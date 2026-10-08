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


@pytest.fixture
def window(qapp, ssh_config: Path):
    from ssh_terminal.services.app_context import AppContext
    from ssh_terminal.ui.main_window import MainWindow

    ctx = AppContext.create()
    ctx.settings.first_run = False
    ctx.resolver.prefer_openssh = False
    w = MainWindow(ctx)
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
