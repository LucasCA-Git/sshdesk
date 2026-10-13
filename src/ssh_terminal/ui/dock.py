"""Drag and dock: drag a terminal (by its header) or a host (from the sidebar) onto
another terminal's edge to place it to the left, right, above or below.

While a drag is running, a transparent :class:`DockOverlay` covers the tab page:
it receives every drag event (so file panes and terminals underneath don't), works
out which pane is under the cursor and which edge is closest, and paints the half
of that pane where the dragged item will land.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from ssh_terminal.ui.theme import current_palette

PANE_MIME = "application/x-sshdesk-pane"  # a pane of this window being moved
HOST_MIME = "application/x-sshdesk-host"  # a host alias dragged from the sidebar


class Zone(Enum):
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"

    @property
    def orientation(self) -> Qt.Orientation:
        return Qt.Orientation.Horizontal if self in (Zone.LEFT, Zone.RIGHT) else Qt.Orientation.Vertical

    @property
    def before(self) -> bool:
        """True when the dropped item goes before the target (left / above)."""
        return self in (Zone.LEFT, Zone.TOP)


def zone_at(rect: QRect, pos: QPoint) -> Zone:
    """Edge of ``rect`` closest to ``pos`` (relative to the rect's size)."""
    w, h = max(rect.width(), 1), max(rect.height(), 1)
    fx, fy = (pos.x() - rect.left()) / w, (pos.y() - rect.top()) / h
    distances = {Zone.LEFT: fx, Zone.RIGHT: 1 - fx, Zone.TOP: fy, Zone.BOTTOM: 1 - fy}
    return min(distances, key=distances.get)


def zone_rect(rect: QRect, zone: Zone) -> QRect:
    """The half of ``rect`` a drop on ``zone`` will occupy."""
    if zone is Zone.LEFT:
        return QRect(rect.left(), rect.top(), rect.width() // 2, rect.height())
    if zone is Zone.RIGHT:
        return QRect(rect.left() + rect.width() // 2, rect.top(), rect.width() - rect.width() // 2, rect.height())
    if zone is Zone.TOP:
        return QRect(rect.left(), rect.top(), rect.width(), rect.height() // 2)
    return QRect(rect.left(), rect.top() + rect.height() // 2, rect.width(), rect.height() - rect.height() // 2)


def accepts(mime: QMimeData) -> bool:
    return mime.hasFormat(PANE_MIME) or mime.hasFormat(HOST_MIME)


class DragHandle(QObject):
    """Event filter that turns press + move on a header into a drag of its pane."""

    def __init__(self, widget: QWidget, on_drag: Callable[[], None]) -> None:
        super().__init__(widget)
        self._on_drag = on_drag
        self._press: QPoint | None = None
        widget.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            self._press = event.position().toPoint() if event.button() == Qt.MouseButton.LeftButton else None
        elif kind == QEvent.Type.MouseMove and isinstance(event, QMouseEvent) and self._press is not None:
            if (event.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._press = None
                self._on_drag()
                return True
        elif kind == QEvent.Type.MouseButtonRelease:
            self._press = None
        return False


class DockOverlay(QWidget):
    """Transparent layer over a tab page that shows and handles the drop zones."""

    def __init__(self, page: QWidget, panes: Callable[[], list[QWidget]],
                 on_drop: Callable[[QMimeData, QWidget, Zone], None]) -> None:
        super().__init__(page)
        self._panes = panes
        self._on_drop = on_drop
        self.source: QWidget | None = None  # pane being moved (None for a host from the sidebar)
        self.target: QWidget | None = None
        self.zone: Zone | None = None
        self.setAcceptDrops(True)
        self.hide()

    # -- lifecycle -------------------------------------------------------- #
    def begin(self, source: QWidget | None = None) -> None:
        self.source = source
        self.target = self.zone = None
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()

    def end(self) -> None:
        self.source = self.target = self.zone = None
        self.hide()

    # -- hit testing ------------------------------------------------------ #
    def _pane_rect(self, pane: QWidget) -> QRect:
        return QRect(pane.mapTo(self.parentWidget(), QPoint(0, 0)), pane.size())

    def locate(self, pos: QPoint) -> tuple[QWidget | None, Zone | None]:
        for pane in self._panes():
            rect = self._pane_rect(pane)
            if pane.isVisible() and rect.contains(pos):
                if pane is self.source:
                    return None, None  # dropping a pane on itself changes nothing
                return pane, zone_at(rect, pos)
        return None, None

    # -- drag events ------------------------------------------------------ #
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        target, zone = self.locate(event.position().toPoint())
        if (target, zone) != (self.target, self.zone):
            self.target, self.zone = target, zone
            self.update()
        if target is not None and accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, _event: QEvent) -> None:  # noqa: N802
        self.target = self.zone = None
        self.update()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        target, zone = self.locate(event.position().toPoint())
        mime = event.mimeData()
        if target is None or zone is None or not accepts(mime):
            event.ignore()
            return
        event.acceptProposedAction()
        self._on_drop(mime, target, zone)
        self.target = self.zone = None
        self.update()

    # -- painting --------------------------------------------------------- #
    def paintEvent(self, _event: QEvent) -> None:  # noqa: N802
        if self.target is None or self.zone is None:
            return
        pal = current_palette()
        rect = zone_rect(self._pane_rect(self.target), self.zone).adjusted(4, 4, -4, -4)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(pal.accent)
        fill.setAlpha(60)
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(pal.accent), 2))
        painter.drawRoundedRect(rect, 8, 8)
