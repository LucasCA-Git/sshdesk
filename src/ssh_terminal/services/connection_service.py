"""Creates terminal sessions (backend factories) from :class:`ConnectionSpec`."""

from __future__ import annotations

import logging
from collections.abc import Callable

from ssh_terminal.errors import HostNotFoundError
from ssh_terminal.models.app_settings import AppSettings
from ssh_terminal.models.connection import ConnectionKind, ConnectionSpec
from ssh_terminal.services.config_service import ConfigService
from ssh_terminal.ssh.config_parser import SSHConfigSet
from ssh_terminal.ssh.ssh_auth import AuthPrompter
from ssh_terminal.ssh.ssh_manager import SSHManager
from ssh_terminal.terminal.backends.base import TerminalBackend
from ssh_terminal.terminal.backends.ssh_backend import SSHBackend
from ssh_terminal.utils.platform import ShellProfile, detect_local_shells, is_windows

log = logging.getLogger(__name__)


class ConnectionService:
    def __init__(
        self,
        config: ConfigService,
        ssh: SSHManager,
        settings: Callable[[], AppSettings],
        prompter: AuthPrompter,
    ) -> None:
        self.config = config
        self.ssh = ssh
        self._settings = settings
        self.prompter = prompter
        self._shells: list[ShellProfile] | None = None

    @property
    def shells(self) -> list[ShellProfile]:
        if self._shells is None:
            self._shells = detect_local_shells()
        return self._shells

    def shell_profile(self, name: str = "") -> ShellProfile:
        wanted = name or self._settings().default_local_shell
        for profile in self.shells:
            if profile.name == wanted:
                return profile
        return self.shells[0]

    # ------------------------------------------------------------------ #
    def backend_factory(self, spec: ConnectionSpec) -> Callable[[], TerminalBackend]:
        if spec.kind is ConnectionKind.LOCAL:
            return lambda: self._local_backend(spec)
        return lambda: self._ssh_backend(spec)

    def _local_backend(self, spec: ConnectionSpec) -> TerminalBackend:
        profile = self.shell_profile(spec.shell)
        settings = self._settings()
        if is_windows():
            from ssh_terminal.terminal.backends.local_windows import WinPtyBackend

            return WinPtyBackend(profile.argv)
        from ssh_terminal.terminal.backends.local_unix import UnixPtyBackend

        return UnixPtyBackend(profile.argv, term_type=settings.term_type)

    def _ssh_backend(self, spec: ConnectionSpec) -> TerminalBackend:
        settings = self._settings()
        self.ssh.options.timeout = settings.connect_timeout
        self.ssh.options.keepalive = settings.keepalive_interval
        self.ssh.options.use_agent = settings.use_agent
        self.ssh.options.allow_keyring = settings.allow_keyring
        self.ssh.options.default_strict = settings.default_strict_host_key_checking

        if spec.adhoc:
            adhoc = spec.adhoc

            def resolve():  # type: ignore[no-untyped-def]
                return self.ssh.resolve_adhoc(adhoc.get("user"), adhoc["hostname"], adhoc.get("port"))
        else:
            alias = spec.alias

            def resolve():  # type: ignore[no-untyped-def]
                # Read-only check on the file itself (runs in a worker thread;
                # the GUI-side cache is refreshed by the file watcher).
                if SSHConfigSet.load(self.config.path).find(alias) is None:
                    raise HostNotFoundError(alias)
                return self.ssh.resolve(alias)

        return SSHBackend(
            resolve,
            lambda progress: self.ssh.connector(self.prompter, progress=progress),
            term_type=settings.term_type,
            apply_config_forwards=settings.apply_config_forwards,
        )
