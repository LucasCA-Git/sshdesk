"""Tiny translation layer.

English text is the key; other languages map it to their wording::

    tr("Containers: {total} (running {running})", total=7, running=5)

The language comes from Settings (``language``: "" = follow the system, "en",
"pt_BR"). Screens read it when they are built, so a change applies to panels
opened afterwards (and to everything after a restart).

Translated today: the Docker and SQL screens and the language setting itself;
the rest of the app is still English-only and moves over gradually.
"""

from __future__ import annotations

import locale
import os

from ssh_terminal.i18n_pt_br import PT_BR

LANGUAGES: dict[str, str] = {"": "Automatic (system)", "en": "English", "pt_BR": "Português (Brasil)"}
_TABLES: dict[str, dict[str, str]] = {"pt_BR": PT_BR}
_current = "en"


def system_language() -> str:
    """``pt_BR`` when the OS speaks Portuguese, English otherwise."""
    candidates = [os.environ.get(var, "") for var in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        from PySide6.QtCore import QLocale

        candidates.insert(0, QLocale.system().name())
    except ImportError:  # pragma: no cover - PySide6 is a hard dependency of the app
        pass
    try:
        candidates.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    return "pt_BR" if any(c.lower().startswith("pt") for c in candidates if c) else "en"


def set_language(code: str) -> str:
    """Apply a Settings value ("" = automatic). Returns the effective language."""
    global _current
    code = code if code in LANGUAGES else ""
    _current = code or system_language()
    return _current


def current_language() -> str:
    return _current


def tr(text: str, **values: object) -> str:
    """Translate ``text`` (English) to the current language and fill ``{placeholders}``."""
    translated = _TABLES.get(_current, {}).get(text, text)
    return translated.format(**values) if values else translated
