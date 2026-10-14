"""Host picker embedded in a menu: a search box over a tree of groups and hosts.

Replaces cascading submenus (one per group / sub-group), which are slow to walk and,
on some Windows setups, did not open at all. Click a group to fold it, click a host
(or press Enter) to pick it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ssh_terminal.i18n import tr
from ssh_terminal.ui.theme import icon

ALIAS_ROLE = Qt.ItemDataRole.UserRole + 1
EXPAND_ALL_UP_TO = 25  # hosts; bigger lists start folded


@dataclass
class HostNode:
    """A host (``alias`` set) or a group (``children``)."""

    label: str
    icon: str = "server"
    alias: str = ""
    search: str = ""  # extra text the filter matches (hostname)
    children: list[HostNode] = field(default_factory=list)

    def host_count(self) -> int:
        return 1 if self.alias else sum(child.host_count() for child in self.children)


def close_popups() -> None:
    """Close the whole menu chain (the picker may sit in a submenu)."""
    for _ in range(10):
        popup = QApplication.activePopupWidget()
        if popup is None:
            return
        popup.close()


class _Search(QLineEdit):
    """Down arrow moves into the tree; Enter picks the first match."""

    def __init__(self, picker: HostPicker) -> None:
        super().__init__()
        self._picker = picker

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Down:
            self._picker.focus_tree()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._picker.pick_first()
            return
        super().keyPressEvent(event)


class HostPicker(QWidget):
    def __init__(self, nodes: list[HostNode], on_pick: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HostPicker")
        self._on_pick = on_pick
        self._picked = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        self.search = _Search(self)
        self.search.setPlaceholderText(tr("Search hosts"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(14)
        self.tree.setExpandsOnDoubleClick(False)  # a click already folds a group
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemActivated.connect(self._activated)
        layout.addWidget(self.tree)
        expand = sum(node.host_count() for node in nodes) <= EXPAND_ALL_UP_TO
        for node in nodes:
            self._add(self.tree.invisibleRootItem(), node, expand)
        rows = self._row_count(self.tree.invisibleRootItem())
        row_height = max(self.tree.sizeHintForRow(0), 22)
        self.tree.setMinimumHeight(min(360, max(4, rows) * row_height + 8))
        self.setMinimumWidth(340)

    # -- building ----------------------------------------------------------- #
    def _add(self, parent: QTreeWidgetItem, node: HostNode, expand: bool) -> None:
        item = QTreeWidgetItem(parent, [node.label])
        item.setIcon(0, icon(node.icon))
        item.setData(0, ALIAS_ROLE, node.alias)
        item.setData(0, Qt.ItemDataRole.ToolTipRole, node.search or node.label)
        item.setData(0, Qt.ItemDataRole.UserRole, f"{node.label} {node.search}".lower())
        for child in node.children:
            self._add(item, child, expand)
        item.setExpanded(expand)

    def _row_count(self, item: QTreeWidgetItem) -> int:
        return sum(1 + (self._row_count(item.child(i)) if item.child(i).isExpanded() else 0)
                   for i in range(item.childCount()))

    def hosts(self, visible_only: bool = False) -> list[QTreeWidgetItem]:
        found: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                child = item.child(i)
                if visible_only and child.isHidden():
                    continue
                if child.data(0, ALIAS_ROLE):
                    found.append(child)
                walk(child)

        walk(self.tree.invisibleRootItem())
        return found

    # -- filtering ----------------------------------------------------------- #
    def _filter(self, text: str) -> None:
        words = text.lower().split()

        def apply(item: QTreeWidgetItem, inherited: bool) -> bool:
            own = all(w in item.data(0, Qt.ItemDataRole.UserRole) for w in words)
            if item.data(0, ALIAS_ROLE):
                visible = inherited or own
            else:  # a matching group shows all its hosts
                visible = False
                for i in range(item.childCount()):
                    visible = apply(item.child(i), inherited or own) or visible
                if words:
                    item.setExpanded(visible)
            item.setHidden(not visible)
            return visible

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            apply(root.child(i), not words)

    # -- picking ------------------------------------------------------------- #
    def focus_tree(self) -> None:
        visible = self.hosts(visible_only=True)
        if visible:
            self.tree.setCurrentItem(visible[0])
        self.tree.setFocus()

    def pick_first(self) -> None:
        visible = self.hosts(visible_only=True)
        if visible:
            self.pick(visible[0].data(0, ALIAS_ROLE))

    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        alias = item.data(0, ALIAS_ROLE)
        if alias:
            self.pick(alias)
        else:
            item.setExpanded(not item.isExpanded())

    def _activated(self, item: QTreeWidgetItem, _column: int) -> None:  # Enter / double click
        alias = item.data(0, ALIAS_ROLE)
        if alias:
            self.pick(alias)

    def pick(self, alias: str) -> None:
        if self._picked:
            return
        self._picked = True
        close_popups()
        QTimer.singleShot(0, lambda: self._on_pick(alias))  # after the menu has gone


def add_host_picker(menu: QMenu, nodes: list[HostNode], on_pick: Callable[[str], None]) -> HostPicker:
    """Put a :class:`HostPicker` inside ``menu``."""
    action = QWidgetAction(menu)
    picker = HostPicker(nodes, on_pick, menu)
    action.setDefaultWidget(picker)
    menu.addAction(action)
    return picker
