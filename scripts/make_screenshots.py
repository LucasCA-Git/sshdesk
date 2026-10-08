"""Render the README screenshots into docs/images/ (offscreen, no network, no real servers).

    QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py

Uses a temporary HOME with a sample ~/.ssh/config, fake terminal backends that
print canned output and a virtual file system for the SFTP panels, so the
images are reproducible and never show anything from your machine.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HOME = Path(tempfile.mkdtemp(prefix="sshdesk-shots-"))
os.environ["HOME"] = os.environ["USERPROFILE"] = str(HOME)
os.environ["XDG_CONFIG_HOME"] = str(HOME / ".config")
os.environ["APPDATA"] = str(HOME / "AppData")

SSH_CONFIG = """\
Host bastion
    HostName 203.0.113.10
    User lucas

Host web-prod-01
    HostName 10.0.1.11
    User deploy
    ProxyJump bastion

Host web-prod-02
    HostName 10.0.1.12
    User deploy
    ProxyJump bastion

Host db-prod
    HostName 10.0.2.20
    User postgres
    ProxyJump bastion

Host api-hml
    HostName 203.0.113.21
    User lucas

Host worker-hml
    HostName 203.0.113.22
    User lucas

Host monitoring
    HostName 203.0.113.23
    User lucas

Host github-personal
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519

Host *
    ServerAliveInterval 60
"""

E = "\x1b["
PROMPT = {
    "web-prod-01": f"{E}1;32mdeploy@web-prod-01{E}0m:{E}1;34m~{E}0m$ ",
    "web-prod-02": f"{E}1;32mdeploy@web-prod-02{E}0m:{E}1;34m~{E}0m$ ",
    "db-prod": f"{E}1;32mpostgres@db-prod{E}0m:{E}1;34m~{E}0m$ ",
    "api-hml": f"{E}1;32mlucas@api-hml{E}0m:{E}1;34m~{E}0m$ ",
}
OUTPUT = {
    "docker": (
        "docker compose ps\r\n"
        f"{E}1mNAME      STATUS       PORTS{E}0m\r\n"
        f"api       {E}32mUp 6 days{E}0m    :8080->8080\r\n"
        f"worker    {E}32mUp 6 days{E}0m\r\n"
        f"redis     {E}32mUp 12 days{E}0m   6379\r\n"
        f"nginx     {E}32mUp 12 days{E}0m   :443->443\r\n"
    ),
    "logs": (
        "tail -n 6 access.log\r\n"
        f"14:02:11 GET  /health          {E}32m200{E}0m   2ms\r\n"
        f"14:02:13 POST /api/v1/orders   {E}32m201{E}0m  41ms\r\n"
        f"14:02:14 GET  /api/v1/items    {E}32m200{E}0m  18ms\r\n"
        f"14:02:15 GET  /login           {E}33m302{E}0m   1ms\r\n"
        f"14:02:17 GET  /api/v1/wallet   {E}31m502{E}0m 300ms\r\n"
        f"14:02:19 GET  /health          {E}32m200{E}0m   2ms\r\n"
    ),
    "psql": (
        "psql -c 'table db_stats'\r\n"
        "  datname  | numbackends | xact_commit\r\n"
        "-----------+-------------+-------------\r\n"
        "  postgres |           1 |      182311\r\n"
        "  app      |          37 |    98113422\r\n"
        "  metrics  |           4 |     4410021\r\n"
        "(3 rows)\r\n"
    ),
    "uptime": "uptime\r\n 14:03:27 up 41 days, load: 0.31 0.27 0.22\r\n",
}


def main() -> None:
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from ssh_terminal import __version__
    from ssh_terminal.models.connection import ConnectionKind, ConnectionSpec, SessionState
    from ssh_terminal.services.app_context import AppContext
    from ssh_terminal.services.file_systems import FileEntry, LocalFS
    from ssh_terminal.services.team_service import TeamInfo
    from ssh_terminal.ui.main_window import MainWindow

    (HOME / ".ssh").mkdir(parents=True)
    (HOME / ".ssh" / "config").write_text(SSH_CONFIG)
    app = QApplication([])  # noqa: F841 - must stay alive while rendering
    OUT.mkdir(parents=True, exist_ok=True)

    def wait(ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    class FakeBackend:
        kind = "ssh"
        user_closed = False
        encoding = "UTF-8"

        def __init__(self, spec: ConnectionSpec) -> None:
            self.spec = spec
            cfg = {"web-prod-01": "deploy@10.0.1.11:22", "web-prod-02": "deploy@10.0.1.12:22",
                   "db-prod": "postgres@10.0.2.20:22", "api-hml": "lucas@203.0.113.21:22"}
            self.description = cfg.get(spec.alias, spec.title)

        def set_callbacks(self, on_data, on_state, on_closed) -> None:
            self.on_state = on_state

        def start(self, cols: int, rows: int) -> None:
            self.on_state(SessionState.CONNECTED, "")

        def write(self, data: bytes) -> None: ...
        def resize(self, cols: int, rows: int) -> None: ...
        def close(self) -> None: ...

    class VirtualFS(LocalFS):
        """A real temp folder shown under a fake absolute path (e.g. /home/deploy)."""

        def __init__(self, real: Path, virtual: str, remote: bool, label: str) -> None:
            self.real, self.virtual, self.remote, self.label = real, virtual, remote, label

        def _r(self, path: str) -> str:
            return str(self.real / path.removeprefix(self.virtual).lstrip("/"))

        def home(self) -> str:
            return self.virtual

        def listdir(self, path: str) -> list[FileEntry]:
            return [FileEntry(e.name, f"{path.rstrip('/')}/{e.name}", e.size, e.is_dir, e.is_link, e.mode, e.mtime)
                    for e in super().listdir(self._r(path))]

        def parent(self, path: str) -> str:
            return path.rsplit("/", 1)[0] or "/"

        def join(self, *parts: str) -> str:
            return "/".join(p.rstrip("/") for p in parts)

    def tree(base: Path, spec: dict) -> None:
        for name, content in spec.items():
            if isinstance(content, dict):
                (base / name).mkdir(parents=True, exist_ok=True)
                tree(base / name, content)
            else:
                (base / name).write_bytes(b"x" * content)

    local_root, remote_root = HOME / "vfs-local", HOME / "vfs-remote"
    tree(local_root, {"Documents": {}, "Downloads": {}, "projects": {"sshdesk": {}}, "release-1.1.0.tar.gz": 4_812_331,
                      "nginx.conf": 2_311, "deploy.sh": 1_204, "notes.md": 6_532})
    tree(remote_root, {"app": {"releases": {}, "shared": {}}, "backups": {}, "logs": {}, ".env": 812,
                       "docker-compose.yml": 3_118, "deploy.log": 288_114, "healthcheck.sh": 640})

    def open_fs(spec: ConnectionSpec, _progress=None) -> LocalFS:
        if spec.kind is ConnectionKind.LOCAL:
            return VirtualFS(local_root, "/home/lucas", False, "Local")
        return VirtualFS(remote_root, "/home/deploy", True, spec.alias)

    ctx = AppContext.create()
    s = ctx.settings
    s.first_run = False
    s.last_seen_version = __version__
    s.font_family = "JetBrains Mono"
    s.font_size = 12
    s.groups = {"Production": ["bastion", "web-prod-01", "web-prod-02", "db-prod"]}
    s.favorites = ["web-prod-01"]
    win = MainWindow(ctx)
    win.connections.backend_factory = lambda spec: (lambda: FakeBackend(spec))
    win.connections.open_file_system = open_fs
    team = TeamInfo(1, "devops", "devops", "owner")
    team_hosts = {"api-hml": "Staging EU", "worker-hml": "Staging EU", "monitoring": ""}

    def refresh_sidebar() -> None:
        win.sidebar.populate(
            ctx.config.hosts(), ctx.config.wildcard_hosts(), s,
            team_of=lambda h: team if h.alias in team_hosts else None,
            editable_teams=[team], team_group_of=lambda a: team_hosts.get(a, ""),
        )

    win._refresh_sidebar = refresh_sidebar
    s.recent_connections = []
    refresh_sidebar()
    win.resize(1360, 800)
    win.show()
    wait(300)

    pending: list = []

    def pane_with(alias: str, *outputs: str, split_from=None, orientation=Qt.Orientation.Horizontal):
        spec = ConnectionSpec.ssh(alias)
        pane = win.split_with(split_from, spec, orientation) if split_from else win.open_spec(spec)
        prompt = PROMPT[alias]
        pending.append((pane, "".join(prompt + OUTPUT[o] for o in outputs) + prompt))
        return pane

    def flush(title: str) -> None:
        """Print the canned output once the layout is final (no re-wrapping)."""
        page = win.tabs.current_page()
        page.custom_title = title
        win.tabs._update_tab(page)
        wait(300)
        while pending:
            pane, text = pending.pop(0)
            pane.terminal.feed(text.encode())
        s.recent_connections = []
        refresh_sidebar()
        wait(400)

    # 1. Hero: two different hosts side by side
    a = pane_with("web-prod-01", "docker", "logs")
    pane_with("db-prod", "psql", split_from=a)
    flush("production")
    win.grab().save(str(OUT / "main-window.png"))

    # 2. Broadcast input to three servers
    b = pane_with("web-prod-01", "uptime")
    c = pane_with("web-prod-02", "uptime", split_from=b)
    pane_with("api-hml", "uptime", split_from=c, orientation=Qt.Orientation.Vertical)
    win.tabs.current_page().arrange_grid()
    win.toggle_broadcast()
    flush("all web servers")
    win.grab().save(str(OUT / "broadcast.png"))

    # 3. Files (SFTP): this computer | server
    right = win.open_files(ConnectionSpec.ssh("web-prod-01"))
    for _ in range(40):
        wait(50)
        if right.cwd:
            break
    wait(400)
    win.grab().save(str(OUT / "sftp.png"))

    # 4. Themes panel
    win.tabs.setCurrentIndex(0)
    win.toggle_appearance()
    win.appearance.current_scheme = "Kanagawa Wave"
    win._scheme_chosen("Kanagawa Wave", "pane")
    wait(500)
    win.grab().save(str(OUT / "themes.png"))
    win.toggle_appearance()

    # 5. What's New
    win.show_whats_new(previous="1.0.0")
    wait(500)
    win._whats_new.grab().save(str(OUT / "whats-new.png"))
    win._whats_new.accept()

    for f in sorted(OUT.glob("*.png")):
        print(f"wrote {f.relative_to(ROOT)}")
    os._exit(0)


if __name__ == "__main__":
    main()
