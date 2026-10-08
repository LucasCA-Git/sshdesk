"""Entry point and command line interface.

Usage::

    sshdesk                     # open the GUI
    sshdesk server-prod         # open the GUI and connect to server-prod
    sshdesk lucas@10.0.0.10:2222  # ad-hoc connection (not in the SSH config)
    sshdesk --local             # open the GUI with a local terminal
    sshdesk --list              # print the hosts from the SSH config and exit
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from ssh_terminal import __app_name__, __version__
from ssh_terminal.models.connection import ConnectionKind, ConnectionSpec
from ssh_terminal.ssh.resolver import resolve_jump_spec

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshdesk", description=f"{__app_name__} — SSH connection manager and terminal")
    parser.add_argument("hosts", nargs="*", metavar="HOST", help="SSH alias(es) or user@host[:port] to open on startup")
    parser.add_argument("-l", "--list", action="store_true", help="list hosts from the SSH config and exit")
    parser.add_argument("--local", action="store_true", help="open a local terminal on startup")
    parser.add_argument("-F", "--config", metavar="PATH", help="use another SSH config file")
    parser.add_argument("--debug", action="store_true", help="verbose logging to the console")
    parser.add_argument("-V", "--version", action="version", version=f"{__app_name__} {__version__}")
    return parser


def list_hosts(config_override: str | None) -> int:
    from ssh_terminal.models.app_settings import AppSettings
    from ssh_terminal.services.app_context import AppContext
    from ssh_terminal.ssh.config_parser import SSHConfigSet

    path, _custom = AppContext.config_path_for(AppSettings(), config_override)
    if not path.exists():
        print(f"No SSH configuration found at {path}", file=sys.stderr)
        return 1
    for host in SSHConfigSet.load(path).concrete_hosts():
        print(host.alias)
    return 0


def spec_for_argument(arg: str, known_aliases: set[str]) -> ConnectionSpec:
    """Alias from the config, or ad-hoc ``user@host[:port]``."""
    if arg in known_aliases:
        return ConnectionSpec.ssh(arg)
    if "@" in arg or ":" in arg:
        user, host, port = resolve_jump_spec(arg)
        return ConnectionSpec(ConnectionKind.SSH, alias="", adhoc={"user": user, "hostname": host, "port": port})
    return ConnectionSpec.ssh(arg)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        return list_hosts(args.config)

    from ssh_terminal.utils.logging import setup_logging

    log_file = setup_logging(debug=args.debug)
    log.info("%s %s starting (log: %s)", __app_name__, __version__, log_file)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(__app_name__)
    app.setApplicationDisplayName(__app_name__)
    app.setOrganizationName(__app_name__)
    app.setApplicationVersion(__version__)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SSHDesk.SSHDesk")  # type: ignore[attr-defined]
        except (AttributeError, OSError) as exc:
            log.debug("AppUserModelID not set: %s", exc)

    from ssh_terminal.services.app_context import AppContext
    from ssh_terminal.ui.main_window import MainWindow, StartupOptions
    from ssh_terminal.ui.theme import app_icon

    app.setWindowIcon(app_icon())
    ctx = AppContext.create(args.config)
    known = {p for h in ctx.config.hosts() for p in h.patterns}
    startup = StartupOptions(config_override=args.config)
    startup.connect = [spec_for_argument(a, known) for a in args.hosts]
    if args.local:
        startup.connect.append(ConnectionSpec.local())

    window = MainWindow(ctx, startup)
    window.show()
    code = app.exec()
    log.info("%s exiting (%s)", __app_name__, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
