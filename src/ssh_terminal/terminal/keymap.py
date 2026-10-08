"""Translate key presses into the byte sequences an xterm sends.

Kept free of widget logic so it can be unit tested. ``key`` and
``modifiers`` are plain ints (``Qt.Key`` / ``Qt.KeyboardModifier`` values).
"""

from __future__ import annotations

from PySide6.QtCore import Qt

ESC = "\x1b"

_CURSOR_KEYS = {
    Qt.Key.Key_Up: "A",
    Qt.Key.Key_Down: "B",
    Qt.Key.Key_Right: "C",
    Qt.Key.Key_Left: "D",
    Qt.Key.Key_Home: "H",
    Qt.Key.Key_End: "F",
}

_TILDE_KEYS = {
    Qt.Key.Key_Insert: 2,
    Qt.Key.Key_Delete: 3,
    Qt.Key.Key_PageUp: 5,
    Qt.Key.Key_PageDown: 6,
    Qt.Key.Key_F5: 15,
    Qt.Key.Key_F6: 17,
    Qt.Key.Key_F7: 18,
    Qt.Key.Key_F8: 19,
    Qt.Key.Key_F9: 20,
    Qt.Key.Key_F10: 21,
    Qt.Key.Key_F11: 23,
    Qt.Key.Key_F12: 24,
}

_SS3_FKEYS = {Qt.Key.Key_F1: "P", Qt.Key.Key_F2: "Q", Qt.Key.Key_F3: "R", Qt.Key.Key_F4: "S"}

_CTRL_SYMBOLS = {
    Qt.Key.Key_At: "\x00",
    Qt.Key.Key_Space: "\x00",
    Qt.Key.Key_2: "\x00",
    Qt.Key.Key_BracketLeft: "\x1b",
    Qt.Key.Key_3: "\x1b",
    Qt.Key.Key_Backslash: "\x1c",
    Qt.Key.Key_4: "\x1c",
    Qt.Key.Key_BracketRight: "\x1d",
    Qt.Key.Key_5: "\x1d",
    Qt.Key.Key_AsciiCircum: "\x1e",
    Qt.Key.Key_6: "\x1e",
    Qt.Key.Key_Underscore: "\x1f",
    Qt.Key.Key_Minus: "\x1f",
    Qt.Key.Key_Slash: "\x1f",
    Qt.Key.Key_7: "\x1f",
    Qt.Key.Key_8: "\x7f",
    Qt.Key.Key_Question: "\x7f",
}


def _modifier_param(modifiers: Qt.KeyboardModifier) -> int:
    """xterm modifier parameter: 1 + shift(1) + alt(2) + ctrl(4)."""
    value = 1
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        value += 1
    if modifiers & Qt.KeyboardModifier.AltModifier:
        value += 2
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        value += 4
    return value


def key_to_bytes(key: int, modifiers: Qt.KeyboardModifier, text: str, app_cursor: bool = False) -> bytes | None:
    """Return the bytes to send for a key press, or ``None`` if not handled."""
    key = Qt.Key(key)
    ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
    alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
    mod = _modifier_param(modifiers)

    def out(s: str) -> bytes:
        return s.encode("utf-8")

    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        return out(ESC + "\r" if alt else "\r")
    if key == Qt.Key.Key_Backspace:
        seq = "\x08" if ctrl else "\x7f"
        return out(ESC + seq if alt else seq)
    if key == Qt.Key.Key_Tab:
        return out(ESC + "[Z" if shift else "\t")
    if key == Qt.Key.Key_Backtab:
        return out(ESC + "[Z")
    if key == Qt.Key.Key_Escape:
        return out(ESC)

    if key in _CURSOR_KEYS:
        letter = _CURSOR_KEYS[key]
        if mod > 1:
            return out(f"{ESC}[1;{mod}{letter}")
        return out(f"{ESC}O{letter}" if app_cursor else f"{ESC}[{letter}")
    if key in _TILDE_KEYS:
        code = _TILDE_KEYS[key]
        return out(f"{ESC}[{code};{mod}~" if mod > 1 else f"{ESC}[{code}~")
    if key in _SS3_FKEYS:
        letter = _SS3_FKEYS[key]
        return out(f"{ESC}[1;{mod}{letter}" if mod > 1 else f"{ESC}O{letter}")

    if ctrl and not alt:
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return bytes([key - Qt.Key.Key_A + 1])
        if key in _CTRL_SYMBOLS:
            return out(_CTRL_SYMBOLS[key])
    if ctrl and alt and Qt.Key.Key_A <= key <= Qt.Key.Key_Z and not text:
        return out(ESC) + bytes([key - Qt.Key.Key_A + 1])

    if text:
        # On Windows AltGr arrives as Ctrl+Alt with printable text (e.g. @ on ABNT2).
        if alt and not ctrl:
            return out(ESC + text)
        return out(text)
    return None
