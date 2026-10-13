"""Translations: every tr() text has a PT-BR entry with the same placeholders."""

from __future__ import annotations

import ast
import string
from pathlib import Path

import pytest

import ssh_terminal.i18n as i18n
from ssh_terminal.i18n_pt_br import PT_BR

SRC = Path(__file__).resolve().parent.parent / "src" / "ssh_terminal"


def tr_texts() -> dict[str, str]:
    """Every literal passed to tr() in the code -> the file it comes from."""
    found: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.setdefault(node.args[0].value, path.name)
    return found


def fields(text: str) -> set[str]:
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


def test_every_text_is_translated_to_portuguese() -> None:
    texts = tr_texts()
    assert texts  # every screen that uses tr() is checked
    missing = {t: f for t, f in texts.items() if t not in PT_BR}
    assert not missing, f"add these to i18n_pt_br.py: {missing}"


@pytest.mark.parametrize("english", sorted(PT_BR))
def test_placeholders_survive_translation(english: str) -> None:
    assert fields(english) == fields(PT_BR[english])


def test_language_switch_and_formatting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert i18n.tr("Containers: {total} (running {running}) · images: {images}", total=7, running=5, images=6) \
        == "Containers: 7 (running 5) · images: 6"
    assert i18n.set_language("pt_BR") == "pt_BR"
    assert i18n.tr("Stop reading") == "Parar leitura"
    assert i18n.tr("{n} rows affected.", n=3) == "3 linhas afetadas."
    assert i18n.tr("Not in the table") == "Not in the table"  # unknown text: English fallback
    monkeypatch.setattr(i18n, "system_language", lambda: "pt_BR")
    assert i18n.set_language("") == "pt_BR"  # automatic follows the system
    assert i18n.set_language("xx") == "pt_BR"  # unknown value = automatic
    assert i18n.set_language("en") == "en" and i18n.tr("Stop reading") == "Stop reading"


def test_system_language_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    real = importlib.reload(i18n)  # undo the conftest patch for this check
    try:
        monkeypatch.setenv("LANG", "pt_BR.UTF-8")
        monkeypatch.setenv("LC_ALL", "")
        assert real.system_language() == "pt_BR"
    finally:
        real.set_language("en")
