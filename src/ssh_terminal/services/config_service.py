"""SSH config CRUD with the "SSH config = source of truth" rule.

Every modification re-reads the file from disk, applies the change to the
fresh document and writes it back atomically (with backup). The in-memory
copy is only a cache for the UI and is refreshed after each write.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from ssh_terminal.errors import ConfigError, DuplicateHostError, HostNotFoundError
from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.ssh import config_writer
from ssh_terminal.ssh.config_parser import SSHConfigDocument, SSHConfigSet
from ssh_terminal.ssh.config_writer import ConfigFileWriter
from ssh_terminal.utils.platform import is_windows

log = logging.getLogger(__name__)

NEW_CONFIG_HEADER = """\
# OpenSSH client configuration
# Created by SSHDesk. See `man ssh_config` for all options.
#
# Host my-server
#     HostName 10.0.0.10
#     User me
#     Port 22
#     IdentityFile ~/.ssh/id_ed25519
"""


def file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class ConfigService:
    def __init__(self, path: Path, writer: ConfigFileWriter) -> None:
        self.path = path
        self.writer = writer
        self._config: SSHConfigSet | None = None
        self._digests: dict[Path, str | None] = {}
        self._texts: dict[Path, str] = {}

    # ------------------------------------------------------------------ #
    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def config(self) -> SSHConfigSet:
        if self._config is None:
            self.reload()
        assert self._config is not None
        return self._config

    def reload(self) -> SSHConfigSet:
        self._config = SSHConfigSet.load(self.path)
        self._remember_state()
        log.info("SSH config loaded: %s (%d hosts)", self.path, len(self._config.concrete_hosts()))
        return self._config

    def _remember_state(self) -> None:
        assert self._config is not None
        self._digests = {}
        self._texts = {}
        for doc in self._config.documents:
            if doc.path is not None:
                self._digests[doc.path] = file_digest(doc.path)
                self._texts[doc.path] = doc.render()
        if self.path not in self._digests:
            self._digests[self.path] = file_digest(self.path)
            self._texts[self.path] = ""

    def watched_files(self) -> list[Path]:
        return list(self._digests) if self._digests else [self.path]

    def changed_files(self) -> list[Path]:
        """Files whose content differs from what we loaded/wrote last."""
        return [p for p, digest in self._digests.items() if file_digest(p) != digest]

    def acknowledge_external_change(self) -> None:
        """Remember the on-disk state without reloading ("Keep Current")."""
        for path in list(self._digests):
            self._digests[path] = file_digest(path)

    def known_text(self, path: Path) -> str:
        return self._texts.get(path, "")

    def hosts(self) -> list[SSHHost]:
        return self.config.concrete_hosts()

    def wildcard_hosts(self) -> list[SSHHost]:
        return self.config.wildcard_hosts()

    def get_host(self, alias: str) -> SSHHost | None:
        return self.config.get_host(alias)

    def aliases(self) -> list[str]:
        return [h.alias for h in self.hosts()]

    # ------------------------------------------------------------------ #
    def create_config_file(self) -> None:
        if self.path.exists():
            return
        self.writer.write(self.path, NEW_CONFIG_HEADER, make_backup=False)
        if not is_windows():
            os.chmod(self.path, 0o600)
        self.reload()

    def _fresh(self) -> SSHConfigSet:
        return SSHConfigSet.load(self.path)

    def _owner(self, fresh: SSHConfigSet, alias: str) -> SSHConfigDocument:
        found = fresh.find(alias)
        if not found:
            raise HostNotFoundError(alias)
        return found[0]

    def _write(self, doc: SSHConfigDocument) -> None:
        if doc.path is None:
            raise ConfigError("Document has no path")
        self.writer.write(doc.path, doc.render())
        self.reload()

    @staticmethod
    def _name_owner(fresh: SSHConfigSet, pattern: str) -> SSHHost | None:
        """Host block (personal or team) already using ``pattern``, compared case-insensitively."""
        wanted = pattern.lower()
        return next((h for h in fresh.hosts() if any(p.lower() == wanted for p in h.patterns)), None)

    def add_host(self, host: SSHHost) -> None:
        fresh = self._fresh()
        for pattern in host.patterns:
            if self._name_owner(fresh, pattern) is not None:
                raise DuplicateHostError(pattern)
        config_writer.add_host(fresh.main, host)
        self._write(fresh.main)
        log.info("Config updated: host %s added", host.alias)

    def update_host(self, original_alias: str, host: SSHHost) -> None:
        fresh = self._fresh()
        doc = self._owner(fresh, original_alias)
        for pattern in host.patterns:
            other = self._name_owner(fresh, pattern)
            if other is not None and original_alias not in other.patterns:
                raise DuplicateHostError(pattern)
        config_writer.update_host(doc, original_alias, host)
        self._write(doc)
        log.info("Config updated: host %s", host.alias)

    def remove_host(self, alias: str) -> None:
        fresh = self._fresh()
        doc = self._owner(fresh, alias)
        config_writer.remove_host(doc, alias)
        self._write(doc)
        log.info("Config updated: host %s removed", alias)

    def rename_host(self, alias: str, new_alias: str) -> None:
        host = self.get_host(alias)
        if host is None:
            raise HostNotFoundError(alias)
        host.alias = new_alias
        self.update_host(alias, host)

    def unique_alias(self, base: str) -> str:
        existing = {p for h in self.config.hosts() for p in h.patterns}
        candidate = f"{base}-copy"
        n = 2
        while candidate in existing:
            candidate = f"{base}-copy{n}"
            n += 1
        return candidate

    def duplicate_host(self, alias: str) -> SSHHost:
        host = self.get_host(alias)
        if host is None:
            raise HostNotFoundError(alias)
        dup = host.clone(self.unique_alias(alias))
        dup.source_file = None
        self.add_host(dup)
        return dup
