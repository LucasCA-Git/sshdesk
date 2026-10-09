"""Terminal emulator and key mapping tests (no GUI needed)."""

from __future__ import annotations

from PySide6.QtCore import Qt

from ssh_terminal.terminal.color_schemes import SCHEMES, get_scheme
from ssh_terminal.terminal.keymap import key_to_bytes
from ssh_terminal.terminal.terminal_emulator import TerminalEmulator

NO = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier


def test_basic_output_and_colors() -> None:
    emu = TerminalEmulator(20, 5)
    emu.feed(b"hello \x1b[31mred\x1b[0m")
    assert emu.screen_text().splitlines()[0] == "hello red"
    assert emu.line(0)[6].fg == "red"
    assert emu.cursor == (9, 0)


def test_utf8_and_wide_chars() -> None:
    emu = TerminalEmulator(20, 3)
    emu.feed("ação ✓ 日本".encode())
    assert emu.screen_text().startswith("ação ✓ 日")


def test_utf8_split_across_chunks() -> None:
    emu = TerminalEmulator(20, 3)
    data = "é".encode()
    emu.feed(data[:1])
    emu.feed(data[1:])
    assert emu.screen_text().startswith("é")


def test_scrollback() -> None:
    emu = TerminalEmulator(10, 3, scrollback=100)
    for i in range(10):
        emu.feed(f"line{i}\r\n".encode())
    assert emu.history_size == 8
    assert emu.line_text(0).rstrip() == "line0"
    assert emu.line_text(emu.history_size).rstrip() == "line8"


def test_clear_scrollback_sequence() -> None:
    emu = TerminalEmulator(10, 3)
    for i in range(10):
        emu.feed(f"l{i}\r\n".encode())
    emu.feed(b"\x1b[3J")
    assert emu.history_size == 0


def test_alt_screen_restores_content() -> None:
    emu = TerminalEmulator(20, 5)
    emu.feed(b"$ prompt")
    emu.feed(b"\x1b[?1049h\x1b[Hvim content")
    assert emu.in_alt_screen
    assert "vim content" in emu.screen_text()
    emu.feed(b"\x1b[?1049l")
    assert not emu.in_alt_screen
    assert emu.screen_text().startswith("$ prompt")
    assert emu.cursor == (8, 0)


def test_alt_screen_does_not_fill_scrollback() -> None:
    emu = TerminalEmulator(10, 3)
    emu.feed(b"\x1b[?1049h")
    for i in range(20):
        emu.feed(f"x{i}\r\n".encode())
    assert emu.history_size == 0


def test_modes() -> None:
    emu = TerminalEmulator()
    assert not emu.app_cursor_keys and not emu.bracketed_paste
    emu.feed(b"\x1b[?1h\x1b[?2004h")
    assert emu.app_cursor_keys and emu.bracketed_paste
    emu.feed(b"\x1b[?1l\x1b[?2004l")
    assert not emu.app_cursor_keys and not emu.bracketed_paste


def test_device_status_report() -> None:
    answers: list[str] = []
    emu = TerminalEmulator(20, 5)
    emu.set_callbacks(on_response=answers.append)
    emu.feed(b"ab\x1b[6n")
    assert answers == ["\x1b[1;3R"]


def test_title_and_bell() -> None:
    titles: list[str] = []
    bells: list[int] = []
    emu = TerminalEmulator()
    emu.set_callbacks(on_title=titles.append, on_bell=lambda: bells.append(1))
    emu.feed(b"\x1b]0;user@host: ~\x07\x07")
    assert titles == ["user@host: ~"] and emu.title == "user@host: ~"
    assert bells == [1]


def test_resize_keeps_cursor_line() -> None:
    emu = TerminalEmulator(20, 10)
    for i in range(9):
        emu.feed(f"row{i}\r\n".encode())
    emu.feed(b"$ ")
    emu.resize(20, 4)
    assert emu.screen_text().splitlines()[-1].startswith("$")
    assert emu.cursor[1] == 3
    assert emu.line_text(0).startswith("row0")
    emu.resize(20, 10)  # grow pulls lines back from the scrollback
    assert emu.screen_text().splitlines()[0].startswith("row0")


def test_text_range() -> None:
    emu = TerminalEmulator(10, 3)
    emu.feed(b"abc\r\ndefgh")
    assert emu.text_range((0, 1), (1, 2)) == "bc\ndef"


def test_backspace_and_cr() -> None:
    emu = TerminalEmulator(10, 2)
    emu.feed(b"abc\x08\x08X\rZ")
    assert emu.screen_text().splitlines()[0] == "ZXc"


def test_keymap_basic() -> None:
    assert key_to_bytes(Qt.Key.Key_Return, NO, "\r") == b"\r"
    assert key_to_bytes(Qt.Key.Key_Backspace, NO, "\x08") == b"\x7f"
    assert key_to_bytes(Qt.Key.Key_C, CTRL, "\x03") == b"\x03"
    assert key_to_bytes(Qt.Key.Key_D, CTRL, "") == b"\x04"
    assert key_to_bytes(Qt.Key.Key_L, CTRL, "") == b"\x0c"
    assert key_to_bytes(Qt.Key.Key_A, NO, "a") == b"a"
    assert key_to_bytes(Qt.Key.Key_unknown, NO, "ç") == "ç".encode()


def test_keymap_navigation() -> None:
    assert key_to_bytes(Qt.Key.Key_Up, NO, "") == b"\x1b[A"
    assert key_to_bytes(Qt.Key.Key_Up, NO, "", app_cursor=True) == b"\x1bOA"
    assert key_to_bytes(Qt.Key.Key_Right, CTRL, "") == b"\x1b[1;5C"
    assert key_to_bytes(Qt.Key.Key_Home, NO, "") == b"\x1b[H"
    assert key_to_bytes(Qt.Key.Key_End, NO, "") == b"\x1b[F"
    assert key_to_bytes(Qt.Key.Key_PageUp, NO, "") == b"\x1b[5~"
    assert key_to_bytes(Qt.Key.Key_PageDown, NO, "") == b"\x1b[6~"
    assert key_to_bytes(Qt.Key.Key_Delete, NO, "") == b"\x1b[3~"
    assert key_to_bytes(Qt.Key.Key_F1, NO, "") == b"\x1bOP"
    assert key_to_bytes(Qt.Key.Key_F12, NO, "") == b"\x1b[24~"
    assert key_to_bytes(Qt.Key.Key_Tab, SHIFT, "") == b"\x1b[Z"


def test_keymap_alt_and_altgr() -> None:
    assert key_to_bytes(Qt.Key.Key_B, ALT, "b") == b"\x1bb"
    # AltGr on Windows (ABNT2 "@" = AltGr+2) arrives as Ctrl+Alt with text.
    assert key_to_bytes(Qt.Key.Key_2, CTRL | ALT, "@") == b"@"


def test_color_schemes() -> None:
    assert {"Default Dark", "Dracula", "Solarized Dark", "Monokai", "Nord", "Gruvbox"} <= set(SCHEMES)
    s = get_scheme("Dracula")
    assert s.color_for("red", "#000") == s.ansi[1]
    assert s.color_for("brightred", "#000") == s.ansi[9]
    assert s.color_for("ff8800", "#000") == "#ff8800"
    assert s.color_for("default", "#123456") == "#123456"
    assert get_scheme("nope").name == "Default Dark"


def test_macos_control_and_command_keys() -> None:
    """On macOS Qt reports the Control key as Meta and ⌘ as Control: the terminal must swap them back."""
    from PySide6.QtCore import Qt

    from ssh_terminal.terminal.keymap import key_to_bytes, terminal_modifiers

    meta, ctrl = Qt.KeyboardModifier.MetaModifier, Qt.KeyboardModifier.ControlModifier
    # physical Control+C on a Mac -> interrupt
    assert key_to_bytes(Qt.Key.Key_C, meta, "", mac=True) == b"\x03"
    assert key_to_bytes(Qt.Key.Key_D, meta, "", mac=True) == b"\x04"
    # Control+Up keeps the xterm modifier parameter (5 = ctrl)
    assert key_to_bytes(Qt.Key.Key_Up, meta, "", mac=True) == b"\x1b[1;5A"
    # ⌘ is never a control key for the terminal
    assert terminal_modifiers(ctrl, mac=True) == Qt.KeyboardModifier.NoModifier
    # other platforms are untouched
    assert key_to_bytes(Qt.Key.Key_C, ctrl, "", mac=False) == b"\x03"
    assert terminal_modifiers(meta | ctrl, mac=False) == meta | ctrl
