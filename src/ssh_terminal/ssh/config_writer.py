"""Surgical edits of :class:`SSHConfigDocument` and safe (atomic) file writing.

Editing rules:

* Only the block being edited is touched. Every other byte of the file is
  preserved.
* Inside the block, option lines whose value did not change are kept
  verbatim (original indentation, keyword spelling, ``=`` separators).
* Options the application does not know are carried in
  ``SSHHost.extra_options`` and therefore survive edits.
* Comments and blank lines inside the block are preserved.

Writing rules: backup first (``config.bak`` next to the file plus a
timestamped copy in the application backup directory), write to a temporary
file in the same directory, ``fsync`` and atomically ``os.replace`` it. On
POSIX the original permission bits are kept (``0600`` for new files).
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from ssh_terminal.errors import ConfigError, DuplicateHostError, HostNotFoundError
from ssh_terminal.models.ssh_host import FALSE_VALUES, TRUE_VALUES, SSHHost
from ssh_terminal.ssh.config_parser import (
    BlockKind,
    ConfigBlock,
    ConfigLine,
    LineKind,
    SSHConfigDocument,
    quote_argument,
    split_arguments,
)
from ssh_terminal.utils.platform import is_windows

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# Value helpers
# ---------------------------------------------------------------------- #
def _normalise(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        v = v[1:-1]
    if v.lower() in TRUE_VALUES or v.lower() in FALSE_VALUES:
        return v.lower()
    return v


def _desired_options(host: SSHHost) -> list[tuple[str, str]]:
    out = []
    for key, value in host.to_options():
        if key.lower() == "identityfile":
            value = quote_argument(value)
        out.append((key, value))
    return out


def _header_value(patterns: list[str]) -> str:
    return " ".join(quote_argument(p) for p in patterns)


# ---------------------------------------------------------------------- #
# Block level operations
# ---------------------------------------------------------------------- #
def sync_block(block: ConfigBlock, host: SSHHost) -> None:
    """Make ``block`` describe ``host`` while changing as few lines as possible."""
    if block.kind is not BlockKind.HOST or block.header is None:
        raise ConfigError("Only Host blocks can be edited")

    if block.patterns != host.patterns:
        block.header = block.header.with_value(_header_value(host.patterns))

    desired = _desired_options(host)
    queues: dict[str, deque[tuple[str, str]]] = defaultdict(deque)
    for key, value in desired:
        queues[key.lower()].append((key, value))

    new_lines: list[ConfigLine] = []
    for line in block.lines:
        if line.kind is not LineKind.OPTION:
            new_lines.append(line)
            continue
        queue = queues.get(line.keyword)
        if not queue:
            continue  # option removed by the user
        _key, value = queue.popleft()
        if _normalise(value) == _normalise(line.value):
            new_lines.append(line)
        else:
            new_lines.append(line.with_value(value))

    leftovers = []
    remaining = {k: len(q) for k, q in queues.items()}
    # Preserve the canonical order of ``to_options`` for appended lines.
    consumed: dict[str, int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)
    for key, _ in desired:
        totals[key.lower()] += 1
    for key, value in desired:
        k = key.lower()
        consumed[k] += 1
        if consumed[k] > totals[k] - remaining.get(k, 0):
            leftovers.append((key, value))

    if leftovers:
        indent = block.indent()
        insert_at = 0
        for idx, line in enumerate(new_lines):
            if line.kind is LineKind.OPTION:
                insert_at = idx + 1
        new = [ConfigLine.option(k, v, indent) for k, v in leftovers]
        new_lines[insert_at:insert_at] = new
    block.lines = new_lines


def build_block(host: SSHHost, indent: str = "    ") -> ConfigBlock:
    header = ConfigLine.option("Host", _header_value(host.patterns), indent="")
    lines = [ConfigLine.option(k, v, indent) for k, v in _desired_options(host)]
    return ConfigBlock(BlockKind.HOST, header=header, lines=lines)


def _ends_with_blank(block: ConfigBlock) -> bool:
    lines = list(block.all_lines())
    return not lines or lines[-1].kind is LineKind.BLANK


def _all_aliases(doc: SSHConfigDocument) -> set[str]:
    return {p for b in doc.host_blocks() for p in b.patterns}


# ---------------------------------------------------------------------- #
# Document level operations
# ---------------------------------------------------------------------- #
def add_host(doc: SSHConfigDocument, host: SSHHost) -> ConfigBlock:
    """Insert a new Host block.

    The block is inserted *before* a trailing ``Host *`` block, because OpenSSH
    uses the first value found: specific hosts must come before catch-all
    defaults, otherwise ``Host *`` values would win.
    """
    for pattern in host.patterns:
        if pattern in _all_aliases(doc):
            raise DuplicateHostError(pattern)

    indent = "    "
    for existing in doc.host_blocks():
        indent = existing.indent()
        break
    block = build_block(host, indent)

    insert_at = len(doc.blocks)
    for idx, existing in enumerate(doc.blocks):
        if existing.kind is BlockKind.HOST and existing.patterns == ["*"]:
            insert_at = idx
            break

    previous = doc.blocks[insert_at - 1] if insert_at > 0 else None
    if previous is not None and not _ends_with_blank(previous):
        block.leading.insert(0, ConfigLine("", LineKind.BLANK))
    if insert_at < len(doc.blocks):
        block.lines.append(ConfigLine("", LineKind.BLANK))
    doc.blocks.insert(insert_at, block)
    doc.has_final_newline = True  # a file we append to must end with a newline
    return block


def update_host(doc: SSHConfigDocument, original_alias: str, host: SSHHost) -> ConfigBlock:
    block = doc.find_block(original_alias)
    if block is None:
        raise HostNotFoundError(original_alias)
    others = _all_aliases(doc) - set(block.patterns)
    for pattern in host.patterns:
        if pattern in others:
            raise DuplicateHostError(pattern)
    sync_block(block, host)
    return block


def remove_host(doc: SSHConfigDocument, alias: str) -> None:
    block = doc.find_block(alias)
    if block is None:
        raise HostNotFoundError(alias)
    idx = doc.blocks.index(block)
    del doc.blocks[idx]
    # Avoid leaving two blank lines where the block used to be.
    if idx > 0 and idx < len(doc.blocks):
        prev, nxt = doc.blocks[idx - 1], doc.blocks[idx]
        nxt_lines = list(nxt.all_lines())
        if _ends_with_blank(prev) and nxt_lines and nxt_lines[0].kind is LineKind.BLANK:
            if nxt.leading and nxt.leading[0].kind is LineKind.BLANK:
                nxt.leading.pop(0)
    elif idx == len(doc.blocks) and idx > 0:
        prev = doc.blocks[idx - 1]
        # Trim trailing blank lines left at the end of the file.
        while prev.lines and prev.lines[-1].kind is LineKind.BLANK and len(prev.lines) > 1 and prev.lines[-2].kind is LineKind.BLANK:
            prev.lines.pop()


def rename_patterns(value: str) -> list[str]:
    """Parse a user-typed alias list ("prod p1") into patterns."""
    return split_arguments(value)


# ---------------------------------------------------------------------- #
# Atomic writing
# ---------------------------------------------------------------------- #
class ConfigFileWriter:
    """Backups + atomic replace of SSH config files."""

    def __init__(self, backup_dir: Path, keep: int = 20) -> None:
        self.backup_dir = backup_dir
        self.keep = max(1, keep)

    def backup(self, path: Path) -> Path | None:
        """Copy ``path`` to ``path.bak`` and to a timestamped file. Returns the latter."""
        if not path.exists():
            return None
        sibling = path.with_name(path.name + ".bak")
        shutil.copy2(path, sibling)
        self._restrict(sibling)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.backup_dir / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, target)
        self._restrict(target)
        self._prune(path.name)
        log.info("SSH config backup created: %s", target)
        return target

    def list_backups(self, name: str = "config") -> list[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob(f"{name}.*.bak"), reverse=True)

    def _prune(self, name: str) -> None:
        for old in self.list_backups(name)[self.keep :]:
            try:
                old.unlink()
            except OSError as exc:
                log.warning("Could not remove old backup %s: %s", old, exc)

    @staticmethod
    def _restrict(path: Path) -> None:
        if not is_windows():
            os.chmod(path, 0o600)

    def write(self, path: Path, text: str, make_backup: bool = True) -> None:
        """Backup and atomically replace ``path`` with ``text``.

        Symlinks are followed so a config managed in a dotfiles repository is
        updated in place instead of being replaced by a regular file.
        """
        target = path.resolve() if path.is_symlink() else path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not is_windows() and target.parent.name == ".ssh":
            try:
                os.chmod(target.parent, 0o700)
            except OSError as exc:
                log.warning("Could not set permissions on %s: %s", target.parent, exc)
        if make_backup:
            self.backup(target)

        mode = 0o600
        if target.exists():
            mode = stat.S_IMODE(target.stat().st_mode)

        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8", errors="surrogateescape"))
                handle.flush()
                os.fsync(handle.fileno())
            if not is_windows():
                os.chmod(tmp, mode)
            self._replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        log.info("SSH config written: %s", target)

    @staticmethod
    def _replace(src: Path, dst: Path) -> None:
        # On Windows os.replace fails if another process (editor, antivirus)
        # holds the file without FILE_SHARE_DELETE; retry briefly.
        attempts = 10 if is_windows() else 1
        for attempt in range(attempts):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.1)
