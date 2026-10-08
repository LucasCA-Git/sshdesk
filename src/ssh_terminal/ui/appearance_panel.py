"""Right-side panel to pick terminal colours and font, with live preview cards."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QFontComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.terminal.color_schemes import SCHEMES
from ssh_terminal.ui.theme import current_palette

ROLE_SCHEME = Qt.ItemDataRole.UserRole + 1


class ThemeCardDelegate(QStyledItemDelegate):
    """Draws a theme as a mini terminal preview + name."""

    def __init__(self, panel: AppearancePanel) -> None:
        super().__init__(panel)
        self.panel = panel

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), 62)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        pal = current_palette()
        scheme = SCHEMES[index.data(ROLE_SCHEME)]
        rect: QRect = option.rect.adjusted(4, 3, -4, -3)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = index.data(ROLE_SCHEME) == self.panel.current_scheme
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(QPen(QColor(scheme.highlight if selected else pal.border), 1.2))
            painter.setBrush(QColor(pal.surface if hovered and not selected else pal.surface_alt))
            painter.drawRoundedRect(QRectF(rect), 8, 8)

        preview = QRectF(rect.left() + 8, rect.top() + 7, 58, rect.height() - 14)
        painter.setPen(QPen(QColor(pal.border), 1))
        painter.setBrush(QColor(scheme.background))
        painter.drawRoundedRect(preview, 5, 5)
        bar_colors = [scheme.highlight, scheme.ansi[4], scheme.foreground]
        for i, color in enumerate(bar_colors):
            width = (40, 28, 34)[i]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(preview.left() + 7, preview.top() + 7 + i * 9, width, 4), 2, 2)

        text_left = int(preview.right()) + 12
        name_font = QFont(option.font)
        name_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(name_font)
        painter.setPen(QColor(scheme.highlight if selected else pal.text))
        painter.drawText(QRect(text_left, rect.top() + 10, rect.right() - text_left, 20),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, scheme.name)
        sub_font = QFont(option.font)
        sub_font.setPointSizeF(max(7.5, sub_font.pointSizeF() * 0.85))
        painter.setFont(sub_font)
        painter.setPen(QColor(pal.muted))
        label = ("Dark" if scheme.dark else "Light") + ("  ·  in use" if selected else "")
        painter.drawText(QRect(text_left, rect.top() + 30, rect.right() - text_left, 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        painter.restore()


class AppearancePanel(QWidget):
    """Theme + font picker. ``scope`` is "all" (app setting) or "pane" (current terminal only)."""

    scheme_chosen = Signal(str, str)  # scheme name, scope
    font_changed = Signal(str, int)  # family, size
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AppearancePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(250)
        self.setMaximumWidth(340)
        self.current_scheme = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("APPEARANCE")
        title.setObjectName("PanelSection")
        top.addWidget(title)
        top.addStretch(1)
        close = QPushButton("✕")
        close.setFlat(True)
        close.setFixedWidth(28)
        close.setToolTip("Hide panel")
        close.clicked.connect(self.close_requested)
        top.addWidget(close)
        layout.addLayout(top)

        font_label = QLabel("FONT")
        font_label.setObjectName("PanelSection")
        layout.addWidget(font_label)
        font_row = QHBoxLayout()
        self.font_family = QFontComboBox()
        self.font_family.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 48)
        self.font_size.setFixedWidth(64)
        font_row.addWidget(self.font_family, 1)
        font_row.addWidget(self.font_size)
        layout.addLayout(font_row)
        self.font_family.currentFontChanged.connect(lambda f: self._emit_font())
        self.font_size.valueChanged.connect(lambda _v: self._emit_font())

        themes_label = QLabel("THEMES")
        themes_label.setObjectName("PanelSection")
        layout.addWidget(themes_label)
        scope_row = QHBoxLayout()
        self.scope_group = QButtonGroup(self)
        self.scope_all = QPushButton("All terminals")
        self.scope_pane = QPushButton("This terminal")
        for i, button in enumerate((self.scope_all, self.scope_pane)):
            button.setCheckable(True)
            self.scope_group.addButton(button, i)
            scope_row.addWidget(button)
        self.scope_all.setChecked(True)
        layout.addLayout(scope_row)

        self.list = QListWidget()
        self.list.setMouseTracking(True)
        self.list.setItemDelegate(ThemeCardDelegate(self))
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        for name in SCHEMES:
            item = QListWidgetItem(name)
            item.setData(ROLE_SCHEME, name)
            self.list.addItem(item)
        self.list.itemClicked.connect(self._picked)
        layout.addWidget(self.list, 1)
        self._loading = False

    @property
    def scope(self) -> str:
        return "pane" if self.scope_pane.isChecked() else "all"

    def load(self, scheme: str, family: str, size: int) -> None:
        self._loading = True
        self.current_scheme = scheme
        self.font_family.setCurrentFont(QFont(family))
        self.font_family.setCurrentText(family)
        self.font_size.setValue(size)
        self._loading = False
        self.list.viewport().update()

    def _emit_font(self) -> None:
        if not self._loading:
            self.font_changed.emit(self.font_family.currentText(), self.font_size.value())

    def _picked(self, item: QListWidgetItem) -> None:
        name = item.data(ROLE_SCHEME)
        self.current_scheme = name
        self.list.viewport().update()
        self.scheme_chosen.emit(name, self.scope)
