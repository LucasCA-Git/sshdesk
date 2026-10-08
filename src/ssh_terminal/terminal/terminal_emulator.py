"""VT100/xterm emulation on top of :mod:`pyte`.

pyte implements the escape-sequence parser and the screen model. This module
adds what a real terminal needs and pyte lacks:

* a scrollback buffer (lines scrolled off the top of a full-screen region),
* the alternate screen (``?1049h``/``?47h``/``?1047h``) used by vim, less, htop,
* bracketed paste and application-cursor-key mode tracking,
* answers to device queries (DA/DSR) sent back to the program,
* resize that keeps the cursor line visible (text is pushed to scrollback).

The emulator is pure Python (no Qt) and is driven by the widget.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable
from typing import Any

import pyte
from pyte.screens import Char, Margins

DECCKM = 1 << 5  # application cursor keys (private mode 1)
BRACKETED_PASTE = 2004 << 5
ALT_SCREEN_MODES = {47, 1047, 1049}
MOUSE_MODES = {1000, 1002, 1003, 1006, 1015}

Line = dict[int, Char]


class _Screen(pyte.Screen):
    """pyte screen with scrollback, alternate screen and callbacks."""

    def __init__(self, columns: int, lines: int, scrollback: int) -> None:
        self.scrollback: deque[Line] = deque(maxlen=max(0, scrollback))
        self._alt_saved: tuple[dict[int, Line], Any, Any] | None = None
        self.on_response: Callable[[str], None] | None = None
        self.on_title: Callable[[str], None] | None = None
        self.on_bell: Callable[[], None] | None = None
        self.mouse_modes: set[int] = set()
        super().__init__(columns, lines)

    # -- scrollback ----------------------------------------------------- #
    @property
    def in_alt_screen(self) -> bool:
        return self._alt_saved is not None

    def index(self) -> None:
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0 and not self.in_alt_screen and self.scrollback.maxlen:
            self.scrollback.append(self.buffer[top])
        super().index()

    def erase_in_display(self, how: int = 0, *args: Any, **kwargs: Any) -> None:
        if how == 3:
            self.scrollback.clear()
        super().erase_in_display(how, *args, **kwargs)

    # -- modes ---------------------------------------------------------- #
    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        if kwargs.get("private"):
            for mode in modes:
                if mode in ALT_SCREEN_MODES:
                    self._enter_alt(save_cursor=mode == 1049)
                elif mode in MOUSE_MODES:
                    self.mouse_modes.add(mode)
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        if kwargs.get("private"):
            for mode in modes:
                if mode in ALT_SCREEN_MODES:
                    self._leave_alt()
                elif mode in MOUSE_MODES:
                    self.mouse_modes.discard(mode)
        super().reset_mode(*modes, **kwargs)

    def _enter_alt(self, save_cursor: bool) -> None:
        if self.in_alt_screen:
            return
        saved_lines = {y: dict(line) for y, line in self.buffer.items()}
        cursor = copy.copy(self.cursor)
        self._alt_saved = (saved_lines, cursor if save_cursor else None, self.margins)
        self.buffer.clear()
        self.margins = None
        self.dirty.update(range(self.lines))

    def _leave_alt(self) -> None:
        if not self.in_alt_screen:
            return
        assert self._alt_saved is not None
        saved_lines, cursor, margins = self._alt_saved
        self._alt_saved = None
        self.buffer.clear()
        for y, line in saved_lines.items():
            if y < self.lines:
                self.buffer[y].update({x: ch for x, ch in line.items() if x < self.columns})
        self.margins = margins
        if cursor is not None:
            self.cursor.x = min(cursor.x, self.columns - 1)
            self.cursor.y = min(cursor.y, self.lines - 1)
            self.cursor.attrs = cursor.attrs
        self.dirty.update(range(self.lines))

    # -- resize ---------------------------------------------------------- #
    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        lines = lines or self.lines
        columns = columns or self.columns
        if lines == self.lines and columns == self.columns:
            return
        if lines != self.lines and not self.in_alt_screen:
            self._resize_lines(lines)
        super().resize(lines, columns)
        self.cursor.x = min(self.cursor.x, self.columns - 1)
        self.cursor.y = min(self.cursor.y, self.lines - 1)

    def _resize_lines(self, lines: int) -> None:
        old = self.lines
        rows = [self.buffer[y] if y in self.buffer else None for y in range(old)]
        if lines < old:
            # Keep the cursor visible: push lines above it into the scrollback.
            drop = max(0, self.cursor.y - (lines - 1))
            for row in rows[:drop]:
                if self.scrollback.maxlen:
                    self.scrollback.append(row if row is not None else {})
            rows = rows[drop : drop + lines]
            self.cursor.y -= drop
        else:
            # Growing: pull lines back from the scrollback, like xterm.
            pull = min(lines - old, len(self.scrollback))
            pulled = [self.scrollback.pop() for _ in range(pull)][::-1]
            rows = pulled + rows
            self.cursor.y += pull
        self.buffer.clear()
        for y, row in enumerate(rows):
            if row:
                self.buffer[y].update(row)
        self.lines = lines
        self.margins = None
        self.dirty.update(range(lines))

    # -- callbacks -------------------------------------------------------- #
    def write_process_input(self, data: str) -> None:
        if self.on_response:
            self.on_response(data)

    def set_title(self, param: str) -> None:
        super().set_title(param)
        if self.on_title:
            self.on_title(param)

    def bell(self, *args: Any) -> None:
        if self.on_bell:
            self.on_bell()

    def report_device_attributes(self, mode: int = 0, **kwargs: Any) -> None:
        # Identify as a VT220-ish xterm (pyte answers VT102 by default).
        if mode == 0 and not kwargs.get("private"):
            self.write_process_input("\x1b[?62;22c")


class TerminalEmulator:
    """Screen state + parser. Feed bytes from the backend, read cells to paint."""

    def __init__(self, columns: int = 80, lines: int = 24, scrollback: int = 10000) -> None:
        self.screen = _Screen(columns, lines, scrollback)
        self.stream = pyte.ByteStream(self.screen)
        self.title = ""

    # -- wiring ----------------------------------------------------------- #
    def set_callbacks(
        self,
        on_response: Callable[[str], None] | None = None,
        on_title: Callable[[str], None] | None = None,
        on_bell: Callable[[], None] | None = None,
    ) -> None:
        self.screen.on_response = on_response
        self.screen.on_title = self._title_hook(on_title)
        self.screen.on_bell = on_bell

    def _title_hook(self, cb: Callable[[str], None] | None) -> Callable[[str], None]:
        def hook(title: str) -> None:
            self.title = title
            if cb:
                cb(title)

        return hook

    # -- data ------------------------------------------------------------- #
    def feed(self, data: bytes) -> None:
        self.stream.feed(data)

    def resize(self, columns: int, lines: int) -> None:
        self.screen.resize(lines=max(1, lines), columns=max(2, columns))

    def set_scrollback(self, size: int) -> None:
        old = list(self.screen.scrollback)
        self.screen.scrollback = deque(old[-size:] if size else [], maxlen=max(0, size))

    def clear(self) -> None:
        """Clear scrollback and screen (keeps the cursor line content at the top)."""
        self.screen.scrollback.clear()
        self.screen.erase_in_display(2)
        self.screen.cursor_position()

    def reset(self) -> None:
        self.screen.scrollback.clear()
        self.screen._alt_saved = None
        self.screen.mouse_modes.clear()
        self.screen.reset()

    # -- state -------------------------------------------------------------- #
    @property
    def columns(self) -> int:
        return self.screen.columns

    @property
    def lines(self) -> int:
        return self.screen.lines

    @property
    def cursor(self) -> tuple[int, int]:
        return self.screen.cursor.x, self.screen.cursor.y

    @property
    def cursor_visible(self) -> bool:
        return not self.screen.cursor.hidden

    @property
    def app_cursor_keys(self) -> bool:
        return DECCKM in self.screen.mode

    @property
    def bracketed_paste(self) -> bool:
        return BRACKETED_PASTE in self.screen.mode

    @property
    def in_alt_screen(self) -> bool:
        return self.screen.in_alt_screen

    @property
    def history_size(self) -> int:
        return len(self.screen.scrollback)

    @property
    def default_char(self) -> Char:
        return self.screen.default_char

    def line(self, absolute: int) -> Line:
        """Line by absolute index: 0..history-1 = scrollback, then the screen."""
        hist = len(self.screen.scrollback)
        if absolute < hist:
            return self.screen.scrollback[absolute]
        y = absolute - hist
        if 0 <= y < self.screen.lines:
            return self.screen.buffer[y]
        return {}

    def line_text(self, absolute: int, start: int = 0, end: int | None = None) -> str:
        line = self.line(absolute)
        end = self.columns if end is None else end
        chars = []
        for x in range(start, end):
            ch = line.get(x)
            data = ch.data if ch is not None else " "
            chars.append(data)
        return "".join(chars)

    def screen_text(self) -> str:
        """Visible screen as text (used by tests and 'select all')."""
        hist = self.history_size
        return "\n".join(self.line_text(hist + y).rstrip() for y in range(self.lines))

    def text_range(self, start: tuple[int, int], end: tuple[int, int]) -> str:
        """Text between two (absolute_line, column) positions, inclusive, trailing spaces trimmed."""
        (l1, c1), (l2, c2) = sorted([start, end])
        out = []
        for ln in range(l1, l2 + 1):
            s = c1 if ln == l1 else 0
            e = c2 + 1 if ln == l2 else self.columns
            out.append(self.line_text(ln, s, e).rstrip())
        return "\n".join(out)
