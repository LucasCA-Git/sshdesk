"""Docker and SQL panes driven through the main window against real servers (see test_live_docker_sql)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

LIVE = os.environ.get("SSHDESK_LIVE_SSHD")
pytestmark = pytest.mark.skipif(not LIVE, reason="set SSHDESK_LIVE_SSHD to run live sshd tests")
SHOTS = os.environ.get("VEYLIX_SHOTS")  # optional folder for screenshots


@pytest.fixture(scope="module")
def qapp():  # noqa: ANN201
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def wait_for(qapp, predicate, seconds: float = 20.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def window(qapp, isolated_home: Path):  # noqa: ANN001
    from ssh_terminal import __version__
    from ssh_terminal.services.app_context import AppContext
    from ssh_terminal.ui.main_window import MainWindow

    d = Path(LIVE)
    known = isolated_home / ".ssh" / "known_hosts"
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text("[127.0.0.1]:2222 " + (d / "hostkey.pub").read_text().split(" ", 2)[0] + " "
                     + (d / "hostkey.pub").read_text().split()[1] + "\n")
    (isolated_home / ".ssh" / "config").write_text(
        f"Host box\n    HostName 127.0.0.1\n    Port 2222\n    User root\n    IdentityFile {d}/client_key\n"
        f"    IdentitiesOnly yes\n    UserKnownHostsFile {known}\n", encoding="utf-8")
    ctx = AppContext.create()
    ctx.settings.first_run = False
    ctx.settings.last_seen_version = __version__
    ctx.settings.use_agent = False
    ctx.settings.language = os.environ.get("VEYLIX_LANG", "en")  # VEYLIX_LANG=pt_BR to see the Portuguese screens
    w = MainWindow(ctx)
    w.resize(1280, 820)
    w.show()
    yield w
    for page in w.tabs.pages():
        for item in page.items():
            item.close_session()
    w.ctx.settings.confirm_close_connected = False
    w.close()


def shot(widget, name: str) -> None:  # noqa: ANN001
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        widget.window().grab().save(str(Path(SHOTS) / f"{name}.png"))


@pytest.mark.skipif(not os.environ.get("VEYLIX_LIVE_DOCKER"), reason="set VEYLIX_LIVE_DOCKER=1")
def test_docker_pane(window, qapp) -> None:  # noqa: ANN001
    pane = window.open_tool("docker", "box")
    wait_for(qapp, lambda: pane.model.items)
    names = {c.name for c in pane.model.items}
    assert {"web", "worker", "paused-one", "done-job"} <= names
    assert "Docker" in pane.heading.text() and "running" in pane.counts.text().lower() or "rodando" in pane.counts.text()

    pane.only_running.setChecked(True)
    assert all(pane.proxy.data(pane.proxy.index(r, 0), pane.proxy.sortRole()) is not None
               for r in range(pane.proxy.rowCount()))
    assert pane.proxy.rowCount() < len(pane.model.items)
    pane.only_running.setChecked(False)
    pane.search.setText("work")
    assert pane.proxy.rowCount() == 1
    pane.search.clear()

    pane._select(next(c.id for c in pane.model.items if c.name == "paused-one"))
    assert pane.action_buttons["unpause"].isEnabled() and not pane.action_buttons["start"].isEnabled()
    pane._select(next(c.id for c in pane.model.items if c.name == "web"))
    assert pane.action_buttons["stop"].isEnabled() and not pane.action_buttons["start"].isEnabled()
    pane.run_action("restart")
    wait_for(qapp, lambda: "done" in pane.toast.text() or "concluído" in pane.toast.text())

    pane._select(next(c.id for c in pane.model.items if c.name == "worker"))
    pane.log_lines.setValue(20)
    pane.show_logs()
    wait_for(qapp, lambda: "tick" in pane.log_view.toPlainText())
    shot(pane, "docker")
    pane.stop_logs()
    assert not pane.log_stop_button.isEnabled()

    pane.tabs.setCurrentIndex(1)
    wait_for(qapp, lambda: pane.image_model.items)
    assert any(i.repository == "veylix-test/busybox" for i in pane.image_model.items)
    shot(pane, "docker-images")


@pytest.mark.parametrize("env,kind", [("VEYLIX_LIVE_MYSQL", "mysql"), ("VEYLIX_LIVE_POSTGRES", "postgres")])
def test_sql_pane(window, qapp, monkeypatch, env: str, kind: str) -> None:  # noqa: ANN001
    if not os.environ.get(env):
        pytest.skip(f"set {env}")
    from PySide6.QtWidgets import QMessageBox

    import ssh_terminal.ui.sql_pane as sp
    from ssh_terminal.services.sql_service import SqlProfile

    creds, port = os.environ[env].rsplit("@", 1)
    user, password = creds.split(":", 1)
    pane = window.open_tool("sql", "box")
    pane._asked = True  # no modal dialog in the test: connect directly
    wait_for(qapp, lambda: pane.runner is not None)
    pane.connect_db(SqlProfile(kind=kind, port=int(port), user=user, database="shop"), password)
    wait_for(qapp, lambda: pane.table_list.count() >= 2)
    assert "SSH" in pane.info.text() and pane.db_combo.currentText() == "shop"

    pane.preview_table("customers")
    wait_for(qapp, lambda: pane.result_model.rows)
    assert pane.result_model.columns[:2] == ["id", "name"] and len(pane.result_model.rows) == 3

    pane.max_rows.setValue(250)
    pane.editor.setPlainText("SELECT * FROM orders ORDER BY id;")
    pane.run()
    wait_for(qapp, lambda: len(pane.result_model.rows) == 250)
    assert pane.result_message.isVisible()  # "showing the first 250 rows"
    shot(pane, f"sql-{kind}")

    asked: list[str] = []
    monkeypatch.setattr(sp.QMessageBox, "warning", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Cancel)
    pane.editor.setPlainText("DELETE FROM customers")
    pane.run()
    assert asked and not pane._running  # confirmation asked, cancelled: nothing ran

    pane.editor.setPlainText("SELEC broken")
    pane.run()
    wait_for(qapp, lambda: pane.result_message.isVisible() and ("rror" in pane.result_message.text()
                                                                 or "Erro" in pane.result_message.text()))

    pane.results.setCurrentIndex(1)
    wait_for(qapp, lambda: pane.history_table.rowCount() >= 3)
    pane.disconnect_db()
    assert pane.table_list.count() == 0 and not pane.run_button.isEnabled()
