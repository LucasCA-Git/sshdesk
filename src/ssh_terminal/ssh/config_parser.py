"""Lossless parser for OpenSSH client configuration files.

Design goals:

* **Round-trip safe**: ``SSHConfigDocument.parse(text).render() == text`` for any
  input, byte for byte (line endings, indentation, comments, unknown options,
  ``Match`` blocks, ``Include`` directives...). Edits only touch the lines of
  the block being edited (see :mod:`ssh_terminal.ssh.config_writer`).
* Understands ``Keyword value``, ``Keyword=value`` and ``Keyword = value``,
  quoted arguments and case-insensitive keywords.
* Does **not** use :mod:`configparser` (the SSH config is not an INI file).
"""

from __future__ import annotations

import glob
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ssh_terminal.models.ssh_host import SSHHost, is_pattern

log = logging.getLogger(__name__)

_OPTION_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z][A-Za-z0-9]*)(?P<sep>\s*=\s*|\s+)(?P<value>.*?)(?P<trail>\s*)$")
_BARE_KEY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z][A-Za-z0-9]*)(?P<trail>\s*)$")


class LineKind(StrEnum):
    BLANK = "blank"
    COMMENT = "comment"
    OPTION = "option"
    INVALID = "invalid"


@dataclass
class ConfigLine:
    """A physical line. ``raw`` never includes the line terminator."""

    raw: str
    kind: LineKind = LineKind.INVALID
    key: str = ""  # original spelling
    value: str = ""
    indent: str = ""
    sep: str = " "

    @property
    def keyword(self) -> str:
        return self.key.lower()

    @classmethod
    def parse(cls, raw: str) -> ConfigLine:
        stripped = raw.strip()
        if not stripped:
            return cls(raw, LineKind.BLANK)
        if stripped.startswith("#"):
            return cls(raw, LineKind.COMMENT)
        m = _OPTION_RE.match(raw)
        if m:
            return cls(raw, LineKind.OPTION, m["key"], m["value"], m["indent"], m["sep"])
        m = _BARE_KEY_RE.match(raw)
        if m:
            return cls(raw, LineKind.OPTION, m["key"], "", m["indent"], " ")
        return cls(raw, LineKind.INVALID)

    @classmethod
    def option(cls, key: str, value: str, indent: str = "    ", sep: str = " ") -> ConfigLine:
        raw = f"{indent}{key}{sep}{value}"
        return cls(raw, LineKind.OPTION, key, value, indent, sep)

    def with_value(self, value: str) -> ConfigLine:
        """Same line (indentation, keyword spelling, separator) with a new value."""
        return ConfigLine.option(self.key, value, self.indent, self.sep)


class BlockKind(StrEnum):
    GLOBAL = "global"  # options before the first Host/Match (apply to every host)
    HOST = "host"
    MATCH = "match"


@dataclass
class ConfigBlock:
    kind: BlockKind
    header: ConfigLine | None = None
    lines: list[ConfigLine] = field(default_factory=list)
    leading: list[ConfigLine] = field(default_factory=list)  # comments glued right above the header

    @property
    def patterns(self) -> list[str]:
        if self.kind is not BlockKind.HOST or self.header is None:
            return []
        return split_arguments(self.header.value)

    @property
    def is_wildcard_only(self) -> bool:
        pats = self.patterns
        return bool(pats) and all(is_pattern(p) for p in pats)

    def options(self) -> list[tuple[str, str]]:
        return [(ln.key, ln.value) for ln in self.lines if ln.kind is LineKind.OPTION]

    def all_lines(self) -> Iterator[ConfigLine]:
        yield from self.leading
        if self.header is not None:
            yield self.header
        yield from self.lines

    def indent(self) -> str:
        for ln in self.lines:
            if ln.kind is LineKind.OPTION and ln.indent:
                return ln.indent
        return "    "

    def leading_comment_text(self) -> str | None:
        texts = [ln.raw.strip().lstrip("#").strip() for ln in self.leading if ln.kind is LineKind.COMMENT]
        return "\n".join(t for t in texts if t) or None


def split_arguments(value: str) -> list[str]:
    """Split an option value into arguments, honouring double quotes (like OpenSSH)."""
    args: list[str] = []
    current: list[str] = []
    in_quotes = False
    had_token = False
    for ch in value:
        if ch == '"':
            in_quotes = not in_quotes
            had_token = True
            continue
        if ch.isspace() and not in_quotes:
            if current or had_token:
                args.append("".join(current))
                current, had_token = [], False
            continue
        current.append(ch)
        had_token = True
    if current or had_token:
        args.append("".join(current))
    return args


def quote_argument(value: str) -> str:
    """Quote a value if it contains whitespace (e.g. Windows paths)."""
    if value and (any(c.isspace() for c in value)) and not (value.startswith('"') and value.endswith('"')):
        return f'"{value}"'
    return value


@dataclass
class SSHConfigDocument:
    """A parsed SSH config file that can be rendered back unchanged."""

    path: Path | None
    blocks: list[ConfigBlock] = field(default_factory=list)
    newline: str = "\n"
    final_newline: str = "\n"
    has_final_newline: bool = True
    bom: bool = False

    # ------------------------------------------------------------------ #
    @classmethod
    def parse(cls, text: str, path: Path | None = None) -> SSHConfigDocument:
        bom = text.startswith("﻿")
        if bom:
            text = text[1:]
        newline = "\r\n" if "\r\n" in text else "\n"
        if text.endswith(newline):
            final = newline
        elif text.endswith("\n"):
            final = "\n"
        else:
            final = ""
        body = text[: len(text) - len(final)]
        # Stray bare "\n" inside a CRLF file stay inside the raw line, so the
        # document still renders byte-for-byte identical.
        raw_lines = body.split(newline) if text else []

        doc = cls(path=path, newline=newline, final_newline=final or newline, has_final_newline=bool(final) or not text, bom=bom)
        current = ConfigBlock(BlockKind.GLOBAL)
        doc.blocks.append(current)
        for raw in raw_lines:
            line = ConfigLine.parse(raw)
            if line.kind is LineKind.OPTION and line.keyword in ("host", "match"):
                leading: list[ConfigLine] = []
                # Comments glued right above a Host line describe that host and
                # travel with it (deleted together). Comments at the top of the
                # file (global section) are never moved: they usually describe
                # the file itself.
                if current.kind is not BlockKind.GLOBAL:
                    while current.lines and current.lines[-1].kind is LineKind.COMMENT:
                        leading.insert(0, current.lines.pop())
                kind = BlockKind.HOST if line.keyword == "host" else BlockKind.MATCH
                current = ConfigBlock(kind, header=line, leading=leading)
                doc.blocks.append(current)
            else:
                current.lines.append(line)
        return doc

    @classmethod
    def load(cls, path: Path) -> SSHConfigDocument:
        if not path.exists():
            return cls(path=path)
        text = path.read_bytes().decode("utf-8", errors="surrogateescape")
        return cls.parse(text, path)

    def render(self) -> str:
        lines = [ln.raw for block in self.blocks for ln in block.all_lines()]
        text = self.newline.join(lines)
        if lines and self.has_final_newline:
            text += self.final_newline
        if self.bom:
            text = "﻿" + text
        return text

    # ------------------------------------------------------------------ #
    @property
    def global_block(self) -> ConfigBlock:
        return self.blocks[0]

    def host_blocks(self) -> list[ConfigBlock]:
        return [b for b in self.blocks if b.kind is BlockKind.HOST]

    def find_block(self, alias: str) -> ConfigBlock | None:
        """Block whose *Host* line names ``alias`` literally (patterns are not expanded)."""
        for block in self.host_blocks():
            if alias in block.patterns:
                return block
        return None

    def hosts(self) -> list[SSHHost]:
        """All Host blocks as models (wildcard blocks have ``is_wildcard=True``)."""
        result = []
        for block in self.host_blocks():
            host = SSHHost.from_options(block.patterns, block.options(), self.path)
            host.comment = block.leading_comment_text()
            result.append(host)
        return result

    def global_options(self) -> list[tuple[str, str]]:
        return self.global_block.options()

    def include_patterns(self) -> list[tuple[ConfigBlock, str]]:
        """``Include`` directives with the block they appear in."""
        found = []
        for block in self.blocks:
            for ln in block.lines:
                if ln.kind is LineKind.OPTION and ln.keyword == "include":
                    for arg in split_arguments(ln.value):
                        found.append((block, arg))
        return found


def resolve_include(pattern: str, ssh_dir: Path) -> list[Path]:
    """Expand an ``Include`` argument (relative paths are relative to ``~/.ssh``)."""
    from ssh_terminal.utils.paths import expand_user_path

    expanded = expand_user_path(pattern)
    if not expanded.is_absolute():
        expanded = ssh_dir / expanded
    matches = sorted(glob.glob(str(expanded)))
    return [Path(m) for m in matches if Path(m).is_file()]


@dataclass
class SSHConfigSet:
    """The main config file plus every file pulled in through ``Include``."""

    main: SSHConfigDocument
    included: list[SSHConfigDocument] = field(default_factory=list)

    @property
    def documents(self) -> list[SSHConfigDocument]:
        return [self.main, *self.included]

    def hosts(self) -> list[SSHHost]:
        result: list[SSHHost] = []
        for doc in self.documents:
            result.extend(doc.hosts())
        return result

    def concrete_hosts(self) -> list[SSHHost]:
        return [h for h in self.hosts() if not h.is_wildcard]

    def wildcard_hosts(self) -> list[SSHHost]:
        return [h for h in self.hosts() if h.is_wildcard]

    def find(self, alias: str) -> tuple[SSHConfigDocument, ConfigBlock] | None:
        for doc in self.documents:
            block = doc.find_block(alias)
            if block is not None:
                return doc, block
        return None

    def get_host(self, alias: str) -> SSHHost | None:
        found = self.find(alias)
        if not found:
            return None
        doc, block = found
        host = SSHHost.from_options(block.patterns, block.options(), doc.path)
        host.comment = block.leading_comment_text()
        return host

    def ordered_blocks(self) -> list[tuple[SSHConfigDocument, ConfigBlock]]:
        """Blocks in OpenSSH evaluation order, with Include files expanded in place."""
        by_path = {d.path: d for d in self.included if d.path}
        ssh_dir = self.main.path.parent if self.main.path else Path.home() / ".ssh"
        out: list[tuple[SSHConfigDocument, ConfigBlock]] = []

        def walk(doc: SSHConfigDocument, depth: int) -> None:
            for block in doc.blocks:
                out.append((doc, block))
                if depth > 16:
                    continue
                for ln in block.lines:
                    if ln.kind is LineKind.OPTION and ln.keyword == "include":
                        for arg in split_arguments(ln.value):
                            for p in resolve_include(arg, ssh_dir):
                                inc = by_path.get(p)
                                if inc is not None:
                                    walk(inc, depth + 1)

        walk(self.main, 0)
        return out

    @classmethod
    def load(cls, path: Path) -> SSHConfigSet:
        main = SSHConfigDocument.load(path)
        included: list[SSHConfigDocument] = []
        seen = {path.resolve()} if path.exists() else set()
        ssh_dir = path.parent

        def collect(doc: SSHConfigDocument, depth: int) -> None:
            if depth > 16:
                log.warning("Include depth limit reached in %s", doc.path)
                return
            for _block, pattern in doc.include_patterns():
                for inc_path in resolve_include(pattern, ssh_dir):
                    real = inc_path.resolve()
                    if real in seen:
                        continue
                    seen.add(real)
                    try:
                        inc_doc = SSHConfigDocument.load(inc_path)
                    except OSError as exc:
                        log.warning("Cannot read included SSH config %s: %s", inc_path, exc)
                        continue
                    included.append(inc_doc)
                    collect(inc_doc, depth + 1)

        collect(main, 0)
        return cls(main, included)
