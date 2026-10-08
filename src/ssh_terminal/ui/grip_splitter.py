"""Splitter whose handles show a visible grip, so panels can always be pulled.

The handle stays on screen even when a side panel is collapsed to width 0,
so the hosts sidebar can be dragged back out (or double-clicked).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSplitter, QSplitterHandle

from ssh_terminal.ui.theme import current_palette


class GripHandle(QSplitterHandle):
    def __init__(self, orientation: Qt.Orientation, parent: GripSplitter) -> None:
        super().__init__(orientation, parent)
        self._hover = False
        self.setMouseTracking(True)
        self.setToolTip("Drag to resize · double-click to show/hide")

    def enterEvent(self, event) -> None:  # noqa: N802, ANN001
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802, ANN001
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802, ANN001
        splitter = self.splitter()
        for i in range(splitter.count()):
            if splitter.handle(i) is self:
                splitter.handle_double_clicked.emit(i)
                break
        event.accept()

    def paintEvent(self, _event) -> None:  # noqa: N802, ANN001
        pal = current_palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        line = QColor(pal.accent if self._hover else pal.border)
        grip = QColor(pal.accent if self._hover else pal.muted)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.orientation() == Qt.Orientation.Horizontal:
            x = r.center().x()
            painter.fillRect(x, r.top(), 1, r.height(), line)
            pill = QRectF(x - 1.5, r.center().y() - 18, 4, 36)
        else:
            y = r.center().y()
            painter.fillRect(r.left(), y, r.width(), 1, line)
            pill = QRectF(r.center().x() - 18, y - 1.5, 36, 4)
        painter.setBrush(grip)
        painter.drawRoundedRect(pill, 2, 2)


class GripSplitter(QSplitter):
    handle_double_clicked = Signal(int)  # index of the widget after the handle

    def createHandle(self) -> QSplitterHandle:  # noqa: N802
        return GripHandle(self.orientation(), self)
