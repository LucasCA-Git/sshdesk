"""The visual terminal: a cell grid painted with QPainter.

Responsibilities: rendering (colours, attributes, cursor, selection),
keyboard/IME input, mouse selection, scrollback scrolling, clipboard and
xterm mouse reporting (SGR 1006). It talks to the outside world only through
signals and :meth:`feed`, so it works with any backend.
"""

from __future__ import annotations

import logging
import math
import re

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QClipboard,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QInputMethodEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QWidget

from ssh_terminal.models.app_settings import AppSettings, font_fallbacks
from ssh_terminal.terminal.color_schemes import ANSI_NAMES, ColorScheme, get_scheme
from ssh_terminal.terminal.keymap import IS_MAC, key_to_bytes
from ssh_terminal.terminal.terminal_emulator import TerminalEmulator

log = logging.getLogger(__name__)

PADDING = 6
FEED_CHUNK = 128 * 1024  # bytes parsed per event-loop tick (keeps the UI responsive)
WORD_CHARS = re.compile(r"[\w\-./~:@%+#=?&]")


class TerminalWidget(QAbstractScrollArea):
    """Terminal view. Emits ``input_ready`` with the bytes typed by the user."""

    input_ready = Signal(bytes)
    user_input = Signal(bytes)  # typed/pasted by the user only (used for broadcast input)
    size_changed = Signal(int, int)  # cols, rows
    title_changed = Signal(str)
    bell_rang = Signal()
    focus_received = Signal()
    context_menu_requested = Signal(QPoint)
    selection_changed = Signal(bool)

    # Shortcut key combinations owned by the application (Ctrl+Shift+C...).
    # Everything else typed in the terminal goes to the remote program.
    reserved_shortcuts: set[int] = set()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.emulator = TerminalEmulator(80, 24, settings.scrollback_lines)
        self.emulator.set_callbacks(
            on_response=lambda s: self.input_ready.emit(s.encode("utf-8")),
            on_title=self.title_changed.emit,
            on_bell=self._on_bell,
        )
        self.scheme_override: str | None = None  # per-terminal theme (None = follow settings)
        self.scheme: ColorScheme = get_scheme(settings.color_scheme)
        self._font_size = settings.font_size
        self._pending = bytearray()
        self._feed_timer = QTimer(self)
        self._feed_timer.setSingleShot(True)
        self._feed_timer.setInterval(0)
        self._feed_timer.timeout.connect(self._process_pending)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(40)
        self._resize_timer.timeout.connect(lambda: self.size_changed.emit(self.emulator.columns, self.emulator.lines))
        self._blink_on = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(530)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._flash = False
        # selection in absolute coordinates (line index incl. scrollback, column)
        self._sel_anchor: tuple[int, int] | None = None
        self._sel_end: tuple[int, int] | None = None
        self._sel_mode = "char"
        self._click_count = 0
        self._last_click_pos = QPoint()
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._reset_clicks)
        self._mouse_button_down: int | None = None

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())
        self.apply_settings(settings)

    # ------------------------------------------------------------------ #
    # Settings / appearance
    # ------------------------------------------------------------------ #
    def apply_settings(self, settings: AppSettings) -> None:
        self.settings = settings
        self.scheme = get_scheme(self.scheme_override or settings.color_scheme)
        self._font_size = settings.font_size
        self.emulator.set_scrollback(settings.scrollback_lines)
        if settings.cursor_blink:
            self._blink_timer.start()
        else:
            self._blink_timer.stop()
            self._blink_on = True
        self._apply_font()

    def _apply_font(self) -> None:
        font = QFont()
        families = [self.settings.font_family] + [f for f in font_fallbacks() if f != self.settings.font_family]
        font.setFamilies(families)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        font.setPointSizeF(max(5.0, float(self._font_size)))
        font.setKerning(False)
        font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        self._font = font
        self._font_bold = QFont(font)
        self._font_bold.setBold(True)
        self._font_italic = QFont(font)
        self._font_italic.setItalic(True)
        self._font_bold_italic = QFont(self._font_bold)
        self._font_bold_italic.setItalic(True)
        metrics = QFontMetricsF(font)
        self._cw = max(1.0, metrics.horizontalAdvance("M"))
        self._ch = max(1.0, math.ceil(metrics.lineSpacing()))
        self._ascent = metrics.ascent()
        self.setFont(font)
        self._recompute_grid()
        self.viewport().update()

    def set_scheme_override(self, name: str | None) -> None:
        self.scheme_override = name
        self.scheme = get_scheme(name or self.settings.color_scheme)
        self.viewport().update()

    def zoom(self, delta: int) -> None:
        self._font_size = max(6, min(48, self._font_size + delta))
        self._apply_font()

    def reset_zoom(self) -> None:
        self._font_size = self.settings.font_size
        self._apply_font()

    @property
    def cell_size(self) -> tuple[float, float]:
        return self._cw, self._ch

    # ------------------------------------------------------------------ #
    # Data in
    # ------------------------------------------------------------------ #
    def feed(self, data: bytes) -> None:
        """Queue output from the backend (parsed on the next event-loop tick)."""
        self._pending.extend(data)
        if not self._feed_timer.isActive():
            self._feed_timer.start()

    def feed_text(self, text: str) -> None:
        self.feed(text.replace("\n", "\r\n").encode("utf-8"))

    def _process_pending(self) -> None:
        if not self._pending:
            return
        chunk = bytes(self._pending[:FEED_CHUNK])
        del self._pending[:FEED_CHUNK]
        scroll = self.verticalScrollBar()
        at_bottom = scroll.value() >= scroll.maximum()
        history_before = self.emulator.history_size
        try:
            self.emulator.feed(chunk)
        except Exception:  # noqa: BLE001 - never let a bad sequence kill the widget
            log.exception("Terminal emulator failed to parse output")
        self._sync_scrollbar(at_bottom, history_before)
        self._blink_on = True
        self.viewport().update()
        if self._pending:
            self._feed_timer.start()

    def _sync_scrollbar(self, stick_to_bottom: bool, history_before: int | None = None) -> None:
        scroll = self.verticalScrollBar()
        history = self.emulator.history_size
        value = scroll.value()
        scroll.blockSignals(True)
        scroll.setRange(0, history)
        scroll.setPageStep(self.emulator.lines)
        scroll.setSingleStep(1)
        if stick_to_bottom:
            scroll.setValue(history)
        else:
            scroll.setValue(value)
        scroll.blockSignals(False)

    @property
    def scroll_offset(self) -> int:
        """Lines scrolled up from the bottom (0 = following output)."""
        scroll = self.verticalScrollBar()
        return max(0, scroll.maximum() - scroll.value())

    def scroll_to_bottom(self) -> None:
        scroll = self.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    def scroll_lines(self, lines: int) -> None:
        scroll = self.verticalScrollBar()
        scroll.setValue(scroll.value() + lines)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """Clear scrollback and screen, then ask the shell to redraw its prompt."""
        self.emulator.clear()
        self._sync_scrollbar(True)
        self.clear_selection()
        self.input_ready.emit(b"\x0c")
        self.viewport().update()

    def reset(self) -> None:
        self.emulator.reset()
        self._sync_scrollbar(True)
        self.clear_selection()
        self.input_ready.emit(b"\x0c")
        self.viewport().update()

    def has_selection(self) -> bool:
        if self._sel_anchor is None or self._sel_end is None:
            return False
        return self._sel_anchor != self._sel_end or self._sel_mode != "char"

    def selected_text(self) -> str:
        if not self.has_selection():
            return ""
        start, end = self._selection_bounds()
        return self.emulator.text_range(start, end)

    def copy(self) -> bool:
        text = self.selected_text()
        if not text:
            return False
        QGuiApplication.clipboard().setText(text)
        return True

    def paste(self, mode: QClipboard.Mode = QClipboard.Mode.Clipboard) -> None:
        text = QGuiApplication.clipboard().text(mode)
        if text:
            self.send_text(text, paste=True)

    def send_text(self, text: str, paste: bool = False) -> None:
        if not text:
            return
        text = text.replace("\r\n", "\r").replace("\n", "\r")
        data = text.encode("utf-8")
        if paste and self.emulator.bracketed_paste:
            data = b"\x1b[200~" + data.replace(b"\x1b[201~", b"") + b"\x1b[201~"
        self.scroll_to_bottom()
        self.input_ready.emit(data)
        self.user_input.emit(data)

    def select_all(self) -> None:
        total = self.emulator.history_size + self.emulator.lines
        self._sel_anchor = (0, 0)
        self._sel_end = (total - 1, self.emulator.columns - 1)
        self._sel_mode = "char"
        self.selection_changed.emit(True)
        self.viewport().update()

    def clear_selection(self) -> None:
        had = self.has_selection()
        self._sel_anchor = self._sel_end = None
        if had:
            self.selection_changed.emit(False)
            self.viewport().update()

    # ------------------------------------------------------------------ #
    # Geometry
    # ------------------------------------------------------------------ #
    def _recompute_grid(self) -> None:
        vp = self.viewport()
        width = max(1, vp.width() - 2 * PADDING)
        height = max(1, vp.height() - 2 * PADDING)
        cols = max(2, int(width / self._cw))
        rows = max(1, int(height / self._ch))
        if (cols, rows) != (self.emulator.columns, self.emulator.lines):
            at_bottom = self.scroll_offset == 0
            self.emulator.resize(cols, rows)
            self._sync_scrollbar(at_bottom)
            self._resize_timer.start()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._recompute_grid()

    def grid_size(self) -> tuple[int, int]:
        return self.emulator.columns, self.emulator.lines

    def _cell_at(self, pos: QPoint | QPointF) -> tuple[int, int]:
        """Absolute (line, column) under a viewport position."""
        col = int((pos.x() - PADDING) / self._cw)
        row = int((pos.y() - PADDING) // self._ch)
        col = max(0, min(self.emulator.columns - 1, col))
        first = self.emulator.history_size - self.scroll_offset
        line = max(0, min(self.emulator.history_size + self.emulator.lines - 1, first + row))
        return line, col

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #
    def _color(self, name: str, default: str) -> QColor:
        return QColor(self.scheme.color_for(name, default))

    def _selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        assert self._sel_anchor is not None and self._sel_end is not None
        start, end = sorted([self._sel_anchor, self._sel_end])
        if self._sel_mode == "word":
            start = (start[0], self._word_start(*start))
            end = (end[0], self._word_end(*end))
        elif self._sel_mode == "line":
            start = (start[0], 0)
            end = (end[0], self.emulator.columns - 1)
        return start, end

    def _is_selected(self, line: int, col: int, bounds: tuple[tuple[int, int], tuple[int, int]] | None) -> bool:
        if bounds is None:
            return False
        (l1, c1), (l2, c2) = bounds
        if line < l1 or line > l2:
            return False
        if l1 == l2:
            return c1 <= col <= c2
        if line == l1:
            return col >= c1
        if line == l2:
            return col <= c2
        return True

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self.viewport())
        emu = self.emulator
        scheme = self.scheme
        default_fg = scheme.foreground
        default_bg = scheme.background
        bg_color = QColor(default_bg)
        painter.fillRect(self.viewport().rect(), bg_color)
        ch = self._ch
        first = emu.history_size - self.scroll_offset
        bounds = self._selection_bounds() if self.has_selection() else None
        selection_bg = QColor(scheme.selection)
        default_char = emu.default_char
        columns = emu.columns
        clip = event.rect()
        row_start = max(0, int((clip.top() - PADDING) // ch))
        row_end = min(emu.lines, int((clip.bottom() - PADDING) // ch) + 1)

        for row in range(row_start, row_end):
            absolute = first + row
            line = emu.line(absolute)
            y = PADDING + row * ch
            baseline = y + self._ascent
            run_text: list[str] = []
            run_start = 0
            run_style: tuple | None = None
            for x in range(columns + 1):
                if x < columns:
                    cell = line.get(x, default_char)
                    fg_name = cell.fg
                    if cell.bold and fg_name in ANSI_NAMES:
                        fg_name = scheme.brighten(fg_name)
                    fg = scheme.color_for(fg_name, default_fg)
                    bg = scheme.color_for(cell.bg, default_bg)
                    if cell.reverse:
                        fg, bg = bg, fg
                    selected = bounds is not None and self._is_selected(absolute, x, bounds)
                    style = (fg, bg, cell.bold, cell.italics, cell.underscore, cell.strikethrough, selected)
                    data = cell.data
                else:
                    style = None
                    data = ""
                if style != run_style or x == columns:
                    if run_style is not None and run_text:
                        self._draw_run(painter, run_start, y, baseline, run_text, run_style, selection_bg, default_bg)
                    run_style = style
                    run_start = x
                    run_text = []
                run_text.append(data)

        self._draw_cursor(painter, first)
        if self._flash:
            painter.fillRect(self.viewport().rect(), QColor(255, 255, 255, 40))
        painter.end()

    def _draw_run(
        self,
        painter: QPainter,
        start: int,
        y: float,
        baseline: float,
        texts: list[str],
        style: tuple,
        selection_bg: QColor,
        default_bg: str,
    ) -> None:
        fg, bg, bold, italic, underline, strike, selected = style
        cw, ch = self._cw, self._ch
        x0 = PADDING + start * cw
        width = len(texts) * cw
        if selected:
            painter.fillRect(QRectF(x0, y, width, ch), selection_bg)
        elif bg != default_bg:
            painter.fillRect(QRectF(x0, y, width, ch), QColor(bg))
        if not any(t.strip() for t in texts) and not underline and not strike:
            return
        if bold and italic:
            painter.setFont(self._font_bold_italic)
        elif bold:
            painter.setFont(self._font_bold)
        elif italic:
            painter.setFont(self._font_italic)
        else:
            painter.setFont(self._font)
        painter.setPen(QColor(fg))
        joined = "".join(texts)
        if joined.isascii() and len(joined) == len(texts):
            painter.drawText(QPointF(x0, baseline), joined)
        else:
            # Non-ASCII glyphs (CJK, box drawing, emoji) are placed cell by
            # cell so fallback fonts with other widths keep the grid aligned.
            for i, t in enumerate(texts):
                if t and t != " ":
                    painter.drawText(QPointF(x0 + i * cw, baseline), t)
        if underline:
            painter.drawLine(QPointF(x0, y + ch - 1.5), QPointF(x0 + width, y + ch - 1.5))
        if strike:
            painter.drawLine(QPointF(x0, y + ch / 2), QPointF(x0 + width, y + ch / 2))

    def _draw_cursor(self, painter: QPainter, first: int) -> None:
        emu = self.emulator
        if not emu.cursor_visible:
            return
        cx, cy = emu.cursor
        row = emu.history_size + cy - first
        if row < 0 or row >= emu.lines:
            return
        x = PADDING + cx * self._cw
        y = PADDING + row * self._ch
        color = QColor(self.scheme.cursor)
        rect = QRectF(x, y, self._cw, self._ch)
        if not self.hasFocus():
            painter.setPen(QPen(color, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            return
        if not self._blink_on:
            return
        style = self.settings.cursor_style
        if style == "underline":
            painter.fillRect(QRectF(x, y + self._ch - 2, self._cw, 2), color)
        elif style == "bar":
            painter.fillRect(QRectF(x, y, 2, self._ch), color)
        else:
            painter.fillRect(rect, color)
            cell = emu.screen.buffer[cy].get(cx)
            if cell is not None and cell.data.strip():
                painter.setFont(self._font_bold if cell.bold else self._font)
                painter.setPen(QColor(self.scheme.background))
                painter.drawText(QPointF(x, y + self._ascent), cell.data)

    def _toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        self._update_cursor_area()

    def _update_cursor_area(self) -> None:
        cx, cy = self.emulator.cursor
        row = cy + self.scroll_offset
        x = int(PADDING + cx * self._cw) - 2
        y = int(PADDING + row * self._ch) - 2
        self.viewport().update(QRect(x, y, int(self._cw * 2) + 4, int(self._ch) + 4))

    def _on_bell(self) -> None:
        self.bell_rang.emit()
        mode = self.settings.bell
        if mode == "sound":
            QApplication.beep()
        elif mode == "visual":
            self._flash = True
            self.viewport().update()
            QTimer.singleShot(90, self._end_flash)

    def _end_flash(self) -> None:
        self._flash = False
        self.viewport().update()

    # ------------------------------------------------------------------ #
    # Keyboard
    # ------------------------------------------------------------------ #
    def _is_reserved(self, event: QKeyEvent) -> bool:
        combo = event.keyCombination().toCombined()
        if combo in self.reserved_shortcuts:
            return True
        mods = event.modifiers()
        ctrl_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        # Ctrl+Shift+<symbol>: keyboard layouts report shifted keys
        # differently (e.g. "|" instead of "\"); leave them to the app.
        if (mods & ctrl_shift) == ctrl_shift and not (Qt.Key.Key_A <= event.key() <= Qt.Key.Key_Z):
            return True
        return False

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ShortcutOverride:
            assert isinstance(event, QKeyEvent)
            if not self._is_reserved(event):
                event.accept()
                return True
            return super().event(event)
        if event.type() == QEvent.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self.keyPressEvent(event)
                return True
        return super().event(event)

    def focusNextPrevChild(self, next: bool) -> bool:  # noqa: N802, A002
        return False

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        shift_only = mods == Qt.KeyboardModifier.ShiftModifier
        if shift_only and key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            page = max(1, self.emulator.lines - 1)
            self.scroll_lines(-page if key == Qt.Key.Key_PageUp else page)
            return
        if shift_only and key == Qt.Key.Key_Home and not self.emulator.in_alt_screen:
            self.verticalScrollBar().setValue(0)
            return
        if shift_only and key == Qt.Key.Key_End and not self.emulator.in_alt_screen:
            self.scroll_to_bottom()
            return
        if shift_only and key == Qt.Key.Key_Insert:
            self.paste()
            return
        if IS_MAC and mods & Qt.KeyboardModifier.ControlModifier:
            # ⌘ on macOS (Qt reports Command as Control): Mac-style editing,
            # never sent to the remote side. The real Control key arrives as
            # Meta and is translated by key_to_bytes.
            if key == Qt.Key.Key_C:
                self.copy()
            elif key == Qt.Key.Key_V:
                self.paste()
            elif key == Qt.Key.Key_A:
                self.select_all()
            elif key == Qt.Key.Key_K:
                self.clear()
            else:
                super().keyPressEvent(event)
            return
        data = key_to_bytes(key, mods, event.text(), self.emulator.app_cursor_keys)
        if data is None:
            super().keyPressEvent(event)
            return
        self.clear_selection()
        self.scroll_to_bottom()
        self._blink_on = True
        self.input_ready.emit(data)
        self.user_input.emit(data)

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:  # noqa: N802
        text = event.commitString()
        if text:
            self.scroll_to_bottom()
            self.input_ready.emit(text.encode("utf-8"))
            self.user_input.emit(text.encode("utf-8"))
        event.accept()

    def inputMethodQuery(self, query: Qt.InputMethodQuery):  # noqa: N802, ANN201
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            cx, cy = self.emulator.cursor
            return QRect(int(PADDING + cx * self._cw), int(PADDING + (cy + self.scroll_offset) * self._ch), int(self._cw), int(self._ch))
        return super().inputMethodQuery(query)

    def focusInEvent(self, event) -> None:  # noqa: N802, ANN001
        super().focusInEvent(event)
        self._blink_on = True
        self.focus_received.emit()
        self.viewport().update()

    def focusOutEvent(self, event) -> None:  # noqa: N802, ANN001
        super().focusOutEvent(event)
        self.viewport().update()

    # ------------------------------------------------------------------ #
    # Mouse
    # ------------------------------------------------------------------ #
    def _mouse_reporting(self, event: QMouseEvent | QWheelEvent) -> bool:
        """True if the remote program asked for mouse events (and Shift is not held)."""
        modes = self.emulator.screen.mouse_modes
        if not modes or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            return False
        return 1006 in modes and bool(modes & {1000, 1002, 1003})

    def _send_mouse(self, button: int, pos: QPointF, release: bool = False) -> None:
        col = int((pos.x() - PADDING) / self._cw) + 1
        row = int((pos.y() - PADDING) // self._ch) + 1
        col = max(1, min(self.emulator.columns, col))
        row = max(1, min(self.emulator.lines, row))
        self.input_ready.emit(f"\x1b[<{button};{col};{row}{'m' if release else 'M'}".encode())

    @staticmethod
    def _button_code(button: Qt.MouseButton) -> int:
        return {Qt.MouseButton.LeftButton: 0, Qt.MouseButton.MiddleButton: 1, Qt.MouseButton.RightButton: 2}.get(button, 0)

    def _reset_clicks(self) -> None:
        self._click_count = 0

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._mouse_reporting(event):
            code = self._button_code(event.button())
            self._mouse_button_down = code
            self._send_mouse(code, event.position())
            return
        button = event.button()
        if button == Qt.MouseButton.LeftButton:
            if (event.position().toPoint() - self._last_click_pos).manhattanLength() > 4:
                self._click_count = 0
            self._click_count = self._click_count % 3 + 1
            self._last_click_pos = event.position().toPoint()
            self._click_timer.start(QApplication.doubleClickInterval())
            cell = self._cell_at(event.position())
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self._sel_anchor is not None:
                self._sel_end = cell
            else:
                self._sel_mode = {1: "char", 2: "word", 3: "line"}[self._click_count]
                self._sel_anchor = cell
                self._sel_end = None if self._click_count == 1 else cell
            self.viewport().update()
        elif button == Qt.MouseButton.MiddleButton:
            mode = QClipboard.Mode.Selection if QGuiApplication.clipboard().supportsSelection() else QClipboard.Mode.Clipboard
            self.paste(mode)
        elif button == Qt.MouseButton.RightButton:
            if self.settings.paste_on_right_click:
                if self.has_selection():
                    self.copy()
                    self.clear_selection()
                else:
                    self.paste()
            else:
                self.context_menu_requested.emit(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._mouse_button_down is not None:
            modes = self.emulator.screen.mouse_modes
            if modes & {1002, 1003}:
                self._send_mouse(32 + self._mouse_button_down, event.position())
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton) or self._sel_anchor is None:
            return
        pos = event.position()
        if pos.y() < 0:
            self.scroll_lines(-1)
        elif pos.y() > self.viewport().height():
            self.scroll_lines(1)
        self._sel_end = self._cell_at(pos)
        if self._sel_mode == "char" and self._sel_end == self._sel_anchor:
            self._sel_end = None
        self.viewport().update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._mouse_button_down is not None:
            self._send_mouse(self._mouse_button_down, event.position(), release=True)
            self._mouse_button_down = None
            return
        if event.button() == Qt.MouseButton.LeftButton:
            text = self.selected_text()
            if text:
                clipboard = QGuiApplication.clipboard()
                if clipboard.supportsSelection():
                    clipboard.setText(text, QClipboard.Mode.Selection)
                if self.settings.copy_on_select:
                    clipboard.setText(text)
            self.selection_changed.emit(self.has_selection())

    def _word_start(self, line: int, col: int) -> int:
        text = self.emulator.line_text(line)
        if col >= len(text) or not WORD_CHARS.match(text[col]):
            return col
        while col > 0 and WORD_CHARS.match(text[col - 1]):
            col -= 1
        return col

    def _word_end(self, line: int, col: int) -> int:
        text = self.emulator.line_text(line)
        if col >= len(text) or not WORD_CHARS.match(text[col]):
            return col
        while col + 1 < len(text) and WORD_CHARS.match(text[col + 1]):
            col += 1
        return col

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            return
        steps = max(1, abs(delta) // 40)
        if self._mouse_reporting(event):
            for _ in range(steps):
                self._send_mouse(64 if delta > 0 else 65, event.position())
            return
        if self.emulator.in_alt_screen:
            seq = key_to_bytes(Qt.Key.Key_Up if delta > 0 else Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier, "", self.emulator.app_cursor_keys)
            if seq:
                self.input_ready.emit(seq * steps)
            return
        self.scroll_lines(-steps if delta > 0 else steps)

    def sizeHint(self):  # noqa: N802, ANN201
        from PySide6.QtCore import QSize

        return QSize(int(80 * self._cw + 2 * PADDING), int(24 * self._ch + 2 * PADDING))

