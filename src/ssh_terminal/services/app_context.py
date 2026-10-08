"""Composition root: builds and wires the application services (no Qt widgets)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ssh_terminal.models.app_settings import AppSettings
from ssh_terminal.services.config_service import ConfigService
from ssh_terminal.services.credential_service import KeyringCredentialStore
from ssh_terminal.services.settings_service import SettingsService
from ssh_terminal.services.team_service import TeamService
from ssh_terminal.ssh.config_writer import ConfigFileWriter
from ssh_terminal.ssh.resolver import SSHConfigResolver
from ssh_terminal.ssh.ssh_manager import SSHManager
from ssh_terminal.ssh.ssh_session import ConnectOptions
from ssh_terminal.utils.paths import expand_user_path, get_backup_dir, get_ssh_config_path


@dataclass
class AppContext:
    settings_service: SettingsService
    config: ConfigService
    credentials: KeyringCredentialStore
    resolver: SSHConfigResolver
    ssh: SSHManager
    teams: TeamService

    @property
    def settings(self) -> AppSettings:
        return self.settings_service.settings

    @staticmethod
    def config_path_for(settings: AppSettings, override: str | None = None) -> tuple[Path, bool]:
        """Effective SSH config path and whether it is a custom (non-default) one."""
        raw = override or settings.ssh_config_path
        if raw:
            return expand_user_path(raw), True
        return get_ssh_config_path(), False

    @classmethod
    def create(cls, config_override: str | None = None) -> AppContext:
        settings_service = SettingsService()
        settings = settings_service.load()
        path, custom = cls.config_path_for(settings, config_override)
        writer = ConfigFileWriter(get_backup_dir(), settings.backup_count)
        config = ConfigService(path, writer)
        credentials = KeyringCredentialStore()
        resolver = SSHConfigResolver(path, prefer_openssh=settings.use_openssh_resolver, custom_path=custom)
        options = ConnectOptions(
            timeout=settings.connect_timeout,
            keepalive=settings.keepalive_interval,
            use_agent=settings.use_agent,
            allow_keyring=settings.allow_keyring,
            default_strict=settings.default_strict_host_key_checking,
        )
        ssh = SSHManager(lambda: config.config, resolver, credentials, options)
        return cls(settings_service, config, credentials, resolver, ssh, TeamService(credentials))

    def apply_settings(self, config_override: str | None = None) -> bool:
        """Propagate saved settings to services. Returns True if the config path changed."""
        settings = self.settings
        path, custom = self.config_path_for(settings, config_override)
        self.config.writer.keep = max(1, settings.backup_count)
        self.resolver.prefer_openssh = settings.use_openssh_resolver
        changed = path != self.config.path
        if changed:
            self.config.path = path
            self.resolver.config_path = path
            self.resolver.custom_path = custom
            self.config.reload()
        return changed
