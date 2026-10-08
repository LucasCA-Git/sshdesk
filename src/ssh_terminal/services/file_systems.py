"""Local and remote (SFTP) file systems behind one small API, plus transfers.

The file browser UI talks only to :class:`FileSystem`. ``LocalFS`` uses the
OS; ``RemoteFS`` wraps a paramiko ``SFTPClient`` opened on an authenticated
:class:`~ssh_terminal.ssh.ssh_session.SSHConnection`.

Paramiko SFTP clients must not be shared between threads doing concurrent
requests, so every transfer opens its own SFTP channel (``RemoteFS.clone``)
on the same SSH transport — no second login.
"""

from __future__ import annotations

import logging
import os
import posixpath
import shutil
import stat
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DRIVES = ""  # Windows pseudo-path: list of drives


class TransferCancelled(Exception):
    pass


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: str
    size: int
    is_dir: bool
    is_link: bool = False
    mode: int = 0
    mtime: float = 0

    @property
    def permissions(self) -> str:
        return stat.filemode(self.mode) if self.mode else ""


def _sorted(entries: Iterable[FileEntry]) -> list[FileEntry]:
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))


class FileSystem:
    """Common API. Paths are plain strings in the file system's own syntax."""

    remote = False
    label = ""

    def home(self) -> str: ...
    def listdir(self, path: str) -> list[FileEntry]: ...
    def stat(self, path: str) -> FileEntry: ...
    def mkdir(self, path: str) -> None: ...
    def rename(self, old: str, new: str) -> None: ...
    def remove_file(self, path: str) -> None: ...
    def rmdir(self, path: str) -> None: ...
    def join(self, *parts: str) -> str: ...
    def parent(self, path: str) -> str: ...
    def basename(self, path: str) -> str: ...
    def close(self) -> None: ...

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except OSError:
            return False

    def remove(self, entry: FileEntry) -> None:
        """Delete a file or a whole directory tree."""
        if entry.is_dir and not entry.is_link:
            for child in self.listdir(entry.path):
                self.remove(child)
            self.rmdir(entry.path)
        else:
            self.remove_file(entry.path)

    def walk_size(self, entry: FileEntry) -> int:
        if entry.is_dir and not entry.is_link:
            return sum(self.walk_size(c) for c in self.listdir(entry.path))
        return entry.size

    def clone(self) -> FileSystem:
        """An instance safe to use from another thread."""
        return self


class LocalFS(FileSystem):
    label = "Local"

    def home(self) -> str:
        return str(Path.home())

    def _entry(self, path: str, name: str | None = None) -> FileEntry:
        st = os.lstat(path)
        is_link = stat.S_ISLNK(st.st_mode)
        is_dir = os.path.isdir(path)
        return FileEntry(name or os.path.basename(path.rstrip("\\/")) or path, path,
                         0 if is_dir else st.st_size, is_dir, is_link, st.st_mode, st.st_mtime)

    def listdir(self, path: str) -> list[FileEntry]:
        if path == DRIVES and sys.platform == "win32":
            drives = os.listdrives() if hasattr(os, "listdrives") else [f"{c}:\\" for c in "CDEFGH" if os.path.exists(f"{c}:\\")]
            return [FileEntry(d.rstrip("\\"), d, 0, True) for d in drives]
        entries = []
        with os.scandir(path) as it:
            for de in it:
                try:
                    entries.append(self._entry(de.path, de.name))
                except OSError:
                    continue
        return _sorted(entries)

    def stat(self, path: str) -> FileEntry:
        return self._entry(path)

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def rename(self, old: str, new: str) -> None:
        os.rename(old, new)

    def remove_file(self, path: str) -> None:
        os.remove(path)

    def rmdir(self, path: str) -> None:
        os.rmdir(path)

    def remove(self, entry: FileEntry) -> None:
        if entry.is_dir and not entry.is_link:
            shutil.rmtree(entry.path)
        else:
            os.remove(entry.path)

    def join(self, *parts: str) -> str:
        return os.path.join(*parts)

    def parent(self, path: str) -> str:
        if path == DRIVES:
            return DRIVES
        if sys.platform == "win32" and os.path.splitdrive(path)[1].strip("\\/") == "":
            return DRIVES  # C:\ -> list of drives
        stripped = path.rstrip("\\/") or path
        return os.path.dirname(stripped) or stripped

    def basename(self, path: str) -> str:
        return os.path.basename(path.rstrip("\\/")) or path


class RemoteFS(FileSystem):
    remote = True

    def __init__(self, connection, client=None) -> None:  # noqa: ANN001 - SSHConnection, SFTPClient
        import paramiko

        self.connection = connection
        self.client = client or paramiko.SFTPClient.from_transport(connection.transport)
        if self.client is None:
            raise paramiko.SSHException("SFTP subsystem unavailable on the server")
        self.label = connection.config.endpoint if hasattr(connection, "config") else "remote"
        self._owns_connection = True

    def home(self) -> str:
        return self.client.normalize(".")

    @staticmethod
    def _from_attr(path: str, attr) -> FileEntry:  # noqa: ANN001
        mode = attr.st_mode or 0
        return FileEntry(posixpath.basename(path.rstrip("/")) or path, path, attr.st_size or 0,
                         stat.S_ISDIR(mode), stat.S_ISLNK(mode), mode, attr.st_mtime or 0)

    def listdir(self, path: str) -> list[FileEntry]:
        entries = []
        for attr in self.client.listdir_attr(path):
            full = posixpath.join(path, attr.filename)
            entry = self._from_attr(full, attr)
            if entry.is_link:  # show links to directories as directories
                try:
                    target = self.client.stat(full)
                    if stat.S_ISDIR(target.st_mode or 0):
                        entry = FileEntry(entry.name, full, 0, True, True, entry.mode, entry.mtime)
                except OSError:
                    pass
            entries.append(entry)
        return _sorted(entries)

    def stat(self, path: str) -> FileEntry:
        return self._from_attr(path, self.client.stat(path))

    def mkdir(self, path: str) -> None:
        self.client.mkdir(path)

    def rename(self, old: str, new: str) -> None:
        try:
            self.client.posix_rename(old, new)
        except OSError:
            self.client.rename(old, new)

    def remove_file(self, path: str) -> None:
        self.client.remove(path)

    def rmdir(self, path: str) -> None:
        self.client.rmdir(path)

    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def parent(self, path: str) -> str:
        return posixpath.dirname(path.rstrip("/")) or "/"

    def basename(self, path: str) -> str:
        return posixpath.basename(path.rstrip("/")) or path

    def clone(self) -> RemoteFS:
        import paramiko

        other = RemoteFS.__new__(RemoteFS)
        other.connection = self.connection
        other.client = paramiko.SFTPClient.from_transport(self.connection.transport)
        other.label = self.label
        other._owns_connection = False
        return other

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            if self._owns_connection:
                self.connection.close()


# ---------------------------------------------------------------------- #
# Transfers
# ---------------------------------------------------------------------- #
@dataclass
class TransferProgress:
    done: int = 0
    total: int = 0
    current: str = ""
    files: int = 0


@dataclass
class Transfer:
    """Copy ``entries`` of ``src`` into directory ``dest_dir`` of ``dst``."""

    src: FileSystem
    entries: list[FileEntry]
    dst: FileSystem
    dest_dir: str
    skip: set[str] = field(default_factory=set)  # names not to overwrite
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def run(self, report: Callable[[TransferProgress], None] | None = None) -> TransferProgress:
        report = report or (lambda _p: None)
        src, dst = self.src.clone(), self.dst.clone()
        progress = TransferProgress()
        try:
            entries = [e for e in self.entries if e.name not in self.skip]
            progress.total = sum(src.walk_size(e) for e in entries)
            report(progress)
            for entry in entries:
                self._copy(src, dst, entry, dst.join(self.dest_dir, entry.name), progress, report)
        finally:
            if src is not self.src:
                src.close()
            if dst is not self.dst:
                dst.close()
        return progress

    def _check(self) -> None:
        if self.cancel_event.is_set():
            raise TransferCancelled("Transfer cancelled")

    def _copy(self, src: FileSystem, dst: FileSystem, entry: FileEntry, target: str,
              progress: TransferProgress, report: Callable[[TransferProgress], None]) -> None:
        self._check()
        if entry.is_dir and not entry.is_link:
            if not dst.exists(target):
                dst.mkdir(target)
            for child in src.listdir(entry.path):
                self._copy(src, dst, child, dst.join(target, child.name), progress, report)
            return
        progress.current = entry.name
        base = progress.done

        def callback(sent: int, _total: int) -> None:
            self._check()
            progress.done = base + sent
            report(progress)

        copy_file(src, dst, entry.path, target, callback)
        progress.done = base + entry.size
        progress.files += 1
        report(progress)


def copy_file(src: FileSystem, dst: FileSystem, src_path: str, dst_path: str,
              callback: Callable[[int, int], None]) -> None:
    if not src.remote and not dst.remote:
        _local_copy(src_path, dst_path, callback)
    elif not src.remote and dst.remote:
        dst.client.put(src_path, dst_path, callback=callback, confirm=True)  # type: ignore[attr-defined]
    elif src.remote and not dst.remote:
        src.client.get(src_path, dst_path, callback=callback)  # type: ignore[attr-defined]
    else:  # remote -> remote: stream through a local temp file
        with tempfile.TemporaryDirectory(prefix="sshdesk-") as tmp:
            local = os.path.join(tmp, "file")
            src.client.get(src_path, local, callback=lambda d, t: callback(d // 2, t))  # type: ignore[attr-defined]
            size = os.path.getsize(local)
            dst.client.put(local, dst_path, callback=lambda d, t: callback(size // 2 + d // 2, t))  # type: ignore[attr-defined]


def _local_copy(src: str, dst: str, callback: Callable[[int, int], None]) -> None:
    total = os.path.getsize(src)
    done = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while chunk := fin.read(1024 * 256):
            fout.write(chunk)
            done += len(chunk)
            callback(done, total)
    shutil.copystat(src, dst, follow_symlinks=True)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
