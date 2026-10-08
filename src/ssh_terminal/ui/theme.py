"""Application themes (Dark / Light / System), stylesheet and icon helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from string import Template

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

from ssh_terminal.utils.paths import resources_dir

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Palette:
    name: str
    window: str
    sidebar: str
    surface: str
    surface_alt: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_text: str
    selection: str
    hover: str
    input: str
    success: str
    warning: str
    danger: str
    tab_active: str


DARK = Palette(
    name="dark",
    window="#16181d",
    sidebar="#1b1e24",
    surface="#21252c",
    surface_alt="#272b33",
    border="#2c313a",
    text="#d8dce3",
    muted="#8a919d",
    accent="#4c8dff",
    accent_hover="#6aa1ff",
    accent_text="#ffffff",
    selection="#2b3f63",
    hover="#252a32",
    input="#14161a",
    success="#3fb950",
    warning="#d6a32b",
    danger="#f0605a",
    tab_active="#14161b",
)

LIGHT = Palette(
    name="light",
    window="#f4f5f7",
    sidebar="#eceef2",
    surface="#ffffff",
    surface_alt="#f0f2f5",
    border="#d6dae1",
    text="#1f2329",
    muted="#69707c",
    accent="#2f6fe4",
    accent_hover="#255bc0",
    accent_text="#ffffff",
    selection="#cfe0ff",
    hover="#e2e6ec",
    input="#ffffff",
    success="#1f9d47",
    warning="#b7791f",
    danger="#d73a49",
    tab_active="#fafafa",
)

STYLESHEET = Template(
    """
QWidget { color: $text; font-size: 13px; }
QMainWindow, QDialog { background: $window; }
QToolTip { background: $surface_alt; color: $text; border: 1px solid $border; padding: 4px 6px; }

QMenuBar { background: $window; border-bottom: 1px solid $border; padding: 2px 4px; }
QMenuBar::item { padding: 4px 10px; border-radius: 4px; background: transparent; }
QMenuBar::item:selected { background: $hover; }
QMenu { background: $surface; border: 1px solid $border; padding: 4px; border-radius: 6px; }
QMenu::item { padding: 6px 28px 6px 22px; border-radius: 4px; }
QMenu::item:selected { background: $selection; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { height: 1px; background: $border; margin: 4px 8px; }

#Sidebar { background: $sidebar; border-right: 1px solid $border; }
#SidebarTitle { color: $muted; font-size: 11px; font-weight: 600; letter-spacing: 1px; }
#SidebarFooter QToolButton, #SidebarFooter QPushButton { text-align: left; padding: 7px 10px; border: none;
    border-radius: 6px; background: transparent; color: $text; }
#SidebarFooter QPushButton:hover { background: $hover; }
#SidebarFooter { border-top: 1px solid $border; }

QTreeWidget, QTreeView, QListWidget, QListView, QTableWidget, QTableView {
    background: transparent; border: none; outline: 0; }
QTreeWidget::item, QListWidget::item { border-radius: 6px; }
QTreeView::branch { background: transparent; image: none; border-image: none; }
QTableWidget, QTableView { background: $input; border: 1px solid $border; border-radius: 6px; gridline-color: $border; }
QHeaderView::section { background: $surface_alt; color: $muted; border: none; border-bottom: 1px solid $border; padding: 5px 8px; }
QTableWidget::item:selected, QListWidget::item:selected { background: $selection; color: $text; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox, QKeySequenceEdit, QFontComboBox {
    background: $input; border: 1px solid $border; border-radius: 6px; padding: 6px 8px;
    selection-background-color: $selection; }
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid $accent; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: $muted; }
QLineEdit#SearchBox { padding: 7px 8px; background: $surface; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: $surface; border: 1px solid $border; selection-background-color: $selection; }
QSpinBox::up-button, QSpinBox::down-button { width: 16px; border: none; }

QPushButton { background: $surface_alt; border: 1px solid $border; border-radius: 6px; padding: 7px 14px; }
QPushButton:hover { background: $hover; border-color: $muted; }
QPushButton:pressed { background: $selection; }
QPushButton:disabled { color: $muted; }
QPushButton[primary="true"] { background: $accent; border-color: $accent; color: $accent_text; font-weight: 600; }
QPushButton[primary="true"]:hover { background: $accent_hover; }
QPushButton[danger="true"] { background: $danger; border-color: $danger; color: #ffffff; font-weight: 600; }
QPushButton[flat="true"] { background: transparent; border: none; }
QToolButton { border: none; border-radius: 5px; padding: 4px; background: transparent; }
QToolButton:hover { background: $hover; }

QCheckBox, QRadioButton { spacing: 8px; }
QCheckBox::indicator, QRadioButton::indicator { width: 15px; height: 15px; border: 1px solid $muted; background: $input; }
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; image: url($icon_check); }
QCheckBox::indicator:indeterminate { background: $surface_alt; border: 1px dashed $muted; image: url($icon_dash); }
QRadioButton::indicator:checked { background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
    stop:0 $accent_text, stop:0.35 $accent_text, stop:0.45 $accent, stop:1 $accent); border-color: $accent; }
QCheckBox:disabled, QRadioButton:disabled { color: $muted; }
QComboBox::down-arrow { image: url($icon_chevron); width: 12px; height: 12px; }
QTabBar::close-button { image: url($icon_close); subcontrol-position: right; margin: 2px; padding: 2px; border-radius: 4px; }
QTabBar::close-button:hover { background: $border; }
QToolButton#TabPlus { margin: 3px 6px; padding: 4px; }
QToolButton::menu-indicator { image: none; width: 0; }
QGroupBox { border: 1px solid $border; border-radius: 8px; margin-top: 14px; padding: 12px 10px 10px 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: $muted; }

QTabWidget::pane { border: none; background: $tab_active; }
QTabBar { background: $window; }
QTabBar::tab { background: transparent; color: $muted; padding: 8px 12px; border: none;
    border-right: 1px solid $border; min-width: 90px; max-width: 240px; }
QTabBar::tab:selected { background: $tab_active; color: $text; border-top: 2px solid $accent; }
QTabBar::tab:hover:!selected { background: $hover; color: $text; }
#DialogTabs QTabBar::tab { min-width: 60px; border-right: none; border-bottom: 2px solid transparent; border-top: none; }
#DialogTabs QTabBar::tab:selected { background: transparent; border-bottom: 2px solid $accent; border-top: none; }
#DialogTabs::pane { background: transparent; border-top: 1px solid $border; }

QStatusBar { background: $sidebar; border-top: 1px solid $border; color: $muted; }
QStatusBar QLabel { color: $muted; padding: 0 6px; }
QStatusBar::item { border: none; }

QSplitter::handle { background: $border; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $border; border-radius: 4px; min-height: 30px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: $muted; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: $border; border-radius: 4px; min-width: 30px; margin: 2px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

#Banner { background: $surface_alt; border-bottom: 1px solid $border; }
#Banner[level="error"] { background: rgba(240, 96, 90, 0.16); border-bottom: 1px solid $danger; }
#Banner[level="warning"] { background: rgba(214, 163, 43, 0.16); border-bottom: 1px solid $warning; }
#Banner[level="info"] { background: $surface_alt; }
#EmptyState QLabel#EmptyTitle { font-size: 20px; font-weight: 600; }
#EmptyState QLabel#EmptySubtitle { color: $muted; }
#Muted, QLabel[muted="true"] { color: $muted; }
#DialogHeader { font-size: 16px; font-weight: 600; }
#SettingsNav { background: $sidebar; border-right: 1px solid $border; padding: 6px; }
#SettingsNav::item { padding: 8px 10px; border-radius: 6px; }
#SettingsNav::item:selected { background: $selection; }
"""
)


def palette_for(mode: str) -> Palette:
    if mode == "light":
        return LIGHT
    if mode == "system":
        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme() if hasattr(hints, "colorScheme") else Qt.ColorScheme.Unknown
        return LIGHT if scheme == Qt.ColorScheme.Light else DARK
    return DARK


_current = DARK


def current_palette() -> Palette:
    return _current


def _themed_icon_file(name: str, color: str) -> str:
    """Write a tinted copy of an SVG icon for use in ``url()`` inside stylesheets."""
    from ssh_terminal.utils.paths import get_app_dir

    source = resources_dir() / "icons" / f"{name}.svg"
    target_dir = get_app_dir() / "theme-cache"
    target = target_dir / f"{name}-{color.lstrip('#')}.svg"
    try:
        if not target.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8").replace("currentColor", color), encoding="utf-8")
    except OSError as exc:
        log.warning("Cannot write themed icon %s: %s", target, exc)
        return ""
    return target.as_posix()


def apply_theme(app: QApplication, mode: str) -> Palette:
    global _current  # noqa: PLW0603 - single process-wide theme
    pal = palette_for(mode)
    _current = pal
    app.setStyle("Fusion")
    qpal = QPalette()
    qpal.setColor(QPalette.ColorRole.Window, QColor(pal.window))
    qpal.setColor(QPalette.ColorRole.WindowText, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.Base, QColor(pal.input))
    qpal.setColor(QPalette.ColorRole.AlternateBase, QColor(pal.surface_alt))
    qpal.setColor(QPalette.ColorRole.Text, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.Button, QColor(pal.surface_alt))
    qpal.setColor(QPalette.ColorRole.ButtonText, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.Highlight, QColor(pal.selection))
    qpal.setColor(QPalette.ColorRole.HighlightedText, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.ToolTipBase, QColor(pal.surface_alt))
    qpal.setColor(QPalette.ColorRole.ToolTipText, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.PlaceholderText, QColor(pal.muted))
    qpal.setColor(QPalette.ColorRole.Link, QColor(pal.accent))
    qpal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(pal.muted))
    qpal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(pal.muted))
    app.setPalette(qpal)
    tokens = dict(pal.__dict__)
    tokens.update(
        icon_check=_themed_icon_file("check", pal.accent_text),
        icon_dash=_themed_icon_file("minus", pal.muted),
        icon_chevron=_themed_icon_file("chevron-down", pal.muted),
        icon_close=_themed_icon_file("x", pal.muted),
    )
    app.setStyleSheet(STYLESHEET.substitute(tokens))
    icon.cache_clear()
    return pal


@lru_cache(maxsize=256)
def icon(name: str, color: str | None = None, size: int = 64) -> QIcon:
    """Load ``resources/icons/<name>.svg`` tinted with ``color`` (theme text by default)."""
    path = resources_dir() / "icons" / f"{name}.svg"
    try:
        svg = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("Missing icon %s", path)
        return QIcon()
    svg = svg.replace("currentColor", color or _current.text)
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    if not pixmap.loadFromData(QByteArray(svg.encode("utf-8")), "SVG"):
        return QIcon()
    return QIcon(pixmap)


def app_icon() -> QIcon:
    result = QIcon()
    base = resources_dir() / "icons"
    for size in (16, 24, 32, 48, 64, 128, 256):
        png = base / f"app-{size}.png"
        if png.exists():
            result.addFile(str(png), QSize(size, size))
    if result.isNull():
        return icon("app", None, 256)
    return result


def status_dot(color: str, size: int = 12) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.end()
    return QIcon(pixmap)
