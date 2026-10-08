"""SFTP on top of an existing :class:`SSHConnection`.

The API is ready for a future file-browser UI; it reuses the authenticated
transport of a terminal tab, so no second login is required.
"""

from __future__ import annotations

import logging
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import paramiko

from ssh_terminal.ssh.ssh_session import SSHConnection

log = logging.getLogger(__name__)

Progress = Callable[[int, int], None]


@dataclass(frozen=True)
class RemoteEntry:
    name: str
    path: str
    size: int
    is_dir: bool
    is_link: bool
    mode: int
    mtime: int

    @property
    def permissions(self) -> str:
        return stat.filemode(self.mode)


class SFTPSession:
    def __init__(self, connection: SSHConnection) -> None:
        self.connection = connection
        self.client = paramiko.SFTPClient.from_transport(connection.transport)
        if self.client is None:
            raise paramiko.SSHException("SFTP subsystem unavailable")
        log.info("SFTP session opened on %s", connection.config.endpoint)

    def home(self) -> str:
        return self.client.normalize(".")

    def listdir(self, path: str = ".") -> list[RemoteEntry]:
        entries = []
        for attr in self.client.listdir_attr(path):
            mode = attr.st_mode or 0
            entries.append(
                RemoteEntry(
                    name=attr.filename,
                    path=str(PurePosixPath(path) / attr.filename),
                    size=attr.st_size or 0,
                    is_dir=stat.S_ISDIR(mode),
                    is_link=stat.S_ISLNK(mode),
                    mode=mode,
                    mtime=attr.st_mtime or 0,
                )
            )
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def download(self, remote: str, local: Path, progress: Progress | None = None) -> None:
        self.client.get(remote, str(local), callback=progress)

    def upload(self, local: Path, remote: str, progress: Progress | None = None) -> None:
        self.client.put(str(local), remote, callback=progress)

    def mkdir(self, path: str) -> None:
        self.client.mkdir(path)

    def remove(self, path: str) -> None:
        self.client.remove(path)

    def rmdir(self, path: str) -> None:
        self.client.rmdir(path)

    def rename(self, old: str, new: str) -> None:
        self.client.posix_rename(old, new)

    def close(self) -> None:
        self.client.close()
