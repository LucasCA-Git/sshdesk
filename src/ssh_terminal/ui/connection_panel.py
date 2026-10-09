"""Sidebar: searchable list of connections (favorites, recent, groups, hosts, SSH defaults)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ssh_terminal.models.app_settings import GROUP_SEP, AppSettings, group_label, normalize_group
from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.services.team_service import TeamInfo
from ssh_terminal.ssh.host_classifier import HostRole, host_role
from ssh_terminal.ui.theme import current_palette, icon

ROLE_KIND = Qt.ItemDataRole.UserRole + 1
ROLE_ALIAS = Qt.ItemDataRole.UserRole + 2
ROLE_SUBTITLE = Qt.ItemDataRole.UserRole + 3
ROLE_SECTION = Qt.ItemDataRole.UserRole + 4
ROLE_FAVORITE = Qt.ItemDataRole.UserRole + 5
ROLE_SEARCH = Qt.ItemDataRole.UserRole + 6
ROLE_TEAM = Qt.ItemDataRole.UserRole + 7  # team name for team-managed hosts
ROLE_TEAM_EDIT = Qt.ItemDataRole.UserRole + 8  # True if the user can edit that team

KIND_SECTION = "section"
KIND_HOST = "host"
KIND_WILDCARD = "wildcard"
KIND_GIT = "git"

SECTION_FAVORITES = "FAVORITES"
SECTION_RECENT = "RECENT"
SECTION_HOSTS = "HOSTS"
SECTION_GIT = "GIT & SERVICES"
SECTION_DEFAULTS = "SSH DEFAULTS"
INDENT = 14  # px per sub-group level


def _depth(index: QModelIndex) -> int:
    depth = 0
    parent = index.parent()
    while parent.isValid():
        depth += 1
        parent = parent.parent()
    return depth


class HostDelegate(QStyledItemDelegate):
    """Two-line rows (alias + user@host) with icon, star and connection dot."""

    def __init__(self, panel: ConnectionPanel) -> None:
        super().__init__(panel)
        self.panel = panel

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        kind = index.data(ROLE_KIND)
        return QSize(option.rect.width(), 28 if kind == KIND_SECTION else 42)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        pal = current_palette()
        kind = index.data(ROLE_KIND)
        rect: QRect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        depth = _depth(index)
        if kind == KIND_SECTION:
            rect = rect.adjusted(depth * INDENT, 0, 0, 0)
            font = QFont(option.font)
            font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
            font.setBold(True)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
            painter.setFont(font)
            painter.setPen(QColor(pal.muted))
            expanded = bool(option.state & QStyle.StateFlag.State_Open)
            chevron = icon("chevron-down" if expanded else "chevron-right", pal.muted)
            chevron.paint(painter, QRect(rect.left() + 4, rect.center().y() - 6, 12, 12))
            painter.drawText(rect.adjusted(20, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, index.data(Qt.ItemDataRole.DisplayRole))
            painter.restore()
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        body = rect.adjusted(4 + max(0, depth - 1) * INDENT, 1, -4, -1)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pal.selection))
            painter.drawRoundedRect(body, 6, 6)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pal.hover))
            painter.drawRoundedRect(body, 6, 6)

        alias = index.data(ROLE_ALIAS)
        favorite = bool(index.data(ROLE_FAVORITE))
        icon_name, icon_color = {
            KIND_WILDCARD: ("globe", pal.warning),
            KIND_GIT: ("git", pal.muted),
        }.get(kind, ("server", pal.accent))
        icon(icon_name, icon_color).paint(painter, QRect(body.left() + 8, body.center().y() - 9, 18, 18))

        right = body.right() - 8
        state = self.panel.connection_state(alias) if kind == KIND_HOST else None
        if state:
            color = {"connected": pal.success, "connecting": pal.warning}.get(state, pal.danger)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPoint(right - 4, body.center().y()), 4, 4)
            right -= 14
        if favorite:
            icon("star-filled", pal.warning).paint(painter, QRect(right - 14, body.center().y() - 7, 14, 14))
            right -= 18

        text_left = body.left() + 34
        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(pal.text))
        title_rect = QRect(text_left, body.top() + 3, right - text_left, body.height() // 2)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                         painter.fontMetrics().elidedText(index.data(Qt.ItemDataRole.DisplayRole), Qt.TextElideMode.ElideRight, title_rect.width()))
        sub_font = QFont(option.font)
        sub_font.setPointSizeF(max(7.5, sub_font.pointSizeF() * 0.88))
        painter.setFont(sub_font)
        painter.setPen(QColor(pal.muted))
        sub_rect = QRect(text_left, body.center().y() + 1, right - text_left, body.height() // 2 - 2)
        subtitle = index.data(ROLE_SUBTITLE) or ""
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                         painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight, sub_rect.width()))
        painter.restore()


class _Tree(QTreeWidget):
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.currentItem()
            if item is not None and item.data(0, ROLE_KIND) != KIND_SECTION:
                self.itemDoubleClicked.emit(item, 0)  # same action as double-click
                return
        super().keyPressEvent(event)


class ConnectionPanel(QWidget):
    connect_requested = Signal(str, bool)  # alias, new_tab
    edit_requested = Signal(str)
    edit_defaults_requested = Signal(str)
    delete_requested = Signal(str)
    duplicate_requested = Signal(str)
    rename_requested = Signal(str)
    copy_command_requested = Signal(str, bool)  # alias, full command
    open_config_requested = Signal(str)
    test_requested = Signal(str)
    favorite_toggled = Signal(str)
    group_requested = Signal(str)  # open "move to group" prompt
    group_set = Signal(str, object)  # alias, group name | None
    role_set = Signal(str, str)  # alias, "server" | "git"
    share_requested = Signal(str, int)  # alias, team id
    team_edit_requested = Signal(str)
    team_delete_requested = Signal(str)
    team_hide_requested = Signal(str)  # alias: remove a team host from my list only
    team_restore_requested = Signal(str)  # team slug: bring hidden hosts back
    team_login_requested = Signal(str)  # alias: set MY username/key for a team host
    team_login_reset_requested = Signal(str)  # alias: back to the team's login
    subgroup_requested = Signal(str)  # parent group ("" = top level): create a (sub-)group
    group_rename_requested = Signal(str)
    group_delete_requested = Signal(str)
    split_view_requested = Signal(list)  # aliases
    share_group_requested = Signal(str, int)  # group name, team id
    open_split_requested = Signal(str, str)  # alias, "right" | "down" (next to the current terminal)
    files_requested = Signal(str, str)  # alias, "tab" | "right" | "down"  (SFTP file browser)
    git_test_requested = Signal(str)
    copy_git_remote_requested = Signal(str)
    new_requested = Signal()
    local_requested = Signal()
    settings_requested = Signal()
    import_requested = Signal()
    reload_requested = Signal()
    section_toggled = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(200)
        self._settings = AppSettings()
        self._states: dict[str, str] = {}
        self._hosts: list[SSHHost] = []
        self._editable_teams: list[TeamInfo] = []
        self._my_login_of: Callable[[str], str] = lambda _a: ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("CONNECTIONS")
        title.setObjectName("SidebarTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.reload_button = QToolButton()
        self.reload_button.setToolTip("Reload SSH config")
        self.reload_button.clicked.connect(self.reload_requested)
        header.addWidget(self.reload_button)
        self.add_button = QToolButton()
        self.add_button.setToolTip("New connection")
        self.add_button.clicked.connect(self.new_requested)
        header.addWidget(self.add_button)
        layout.addLayout(header)

        self.search = QLineEdit()
        self.search.setObjectName("SearchBox")
        self.search.setPlaceholderText("Search...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        self.search.returnPressed.connect(self._connect_first_match)
        self._search_action = self.search.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        layout.addWidget(self.search)

        self.tree = _Tree()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(0)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(False)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)  # Ctrl/Shift+click
        self.tree.setMouseTracking(True)
        self.tree.setItemDelegate(HostDelegate(self))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemExpanded.connect(lambda it: self._section_state(it, True))
        self.tree.itemCollapsed.connect(lambda it: self._section_state(it, False))
        layout.addWidget(self.tree, 1)

        self.empty_label = QLabel("No SSH hosts found.\nCreate one with “New Connection”.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setProperty("muted", True)
        self.empty_label.setWordWrap(True)
        self.empty_label.hide()
        layout.addWidget(self.empty_label)

        footer = QWidget()
        footer.setObjectName("SidebarFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 6, 0, 0)
        footer_layout.setSpacing(2)
        self.new_button = QPushButton("  New Connection")
        self.new_button.clicked.connect(self.new_requested)
        self.local_button = QPushButton("  Local Terminal  ▾")
        self.local_button.clicked.connect(self.local_requested)
        self.import_button = QPushButton("  Import SSH Config")
        self.import_button.clicked.connect(self.import_requested)
        self.settings_button = QPushButton("  Settings")
        self.settings_button.clicked.connect(self.settings_requested)
        for button in (self.new_button, self.local_button, self.import_button, self.settings_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            footer_layout.addWidget(button)
        layout.addWidget(footer)
        self.refresh_icons()

    # ------------------------------------------------------------------ #
    def refresh_icons(self) -> None:
        pal = current_palette()
        self.reload_button.setIcon(icon("refresh", pal.muted))
        self.add_button.setIcon(icon("plus", pal.muted))
        self.new_button.setIcon(icon("plus", pal.accent))
        self.local_button.setIcon(icon("terminal", pal.text))
        self.import_button.setIcon(icon("file", pal.text))
        self.settings_button.setIcon(icon("settings", pal.text))
        self._search_action.setIcon(icon("search", pal.muted))
        self.tree.viewport().update()

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def connection_state(self, alias: str) -> str | None:
        return self._states.get(alias)

    def set_connection_states(self, states: dict[str, str]) -> None:
        self._states = states
        self.tree.viewport().update()

    def selected_alias(self) -> str | None:
        item = self.tree.currentItem()
        if item is not None and item.data(0, ROLE_KIND) in (KIND_HOST, KIND_WILDCARD):
            return item.data(0, ROLE_ALIAS)
        return None

    # ------------------------------------------------------------------ #
    def populate(
        self,
        hosts: list[SSHHost],
        defaults: list[SSHHost],
        settings: AppSettings,
        team_of: Callable[[SSHHost], TeamInfo | None] | None = None,
        editable_teams: list[TeamInfo] | None = None,
        team_group_of: Callable[[str], str] | None = None,
        my_login_of: Callable[[str], str] | None = None,
    ) -> None:
        self._settings = settings
        self._my_login_of = my_login_of or (lambda _a: "")
        self._editable_teams = editable_teams or []
        team_of = team_of or (lambda _h: None)
        team_group_of = team_group_of or (lambda _a: "")
        teams_by_alias = {h.alias: t for h in hosts if (t := team_of(h)) is not None}
        self._hosts = hosts
        selected = self.selected_alias()
        by_alias = {h.alias: h for h in hosts}
        self.tree.clear()

        def section(name: str, key: str | None = None, parent: QTreeWidgetItem | None = None) -> QTreeWidgetItem:
            item = QTreeWidgetItem([name])
            item.setData(0, ROLE_KIND, KIND_SECTION)
            item.setData(0, ROLE_SECTION, key or name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            return item

        def group_tree(paths: list[str], key_prefix: str, root: QTreeWidgetItem | None,
                       title: Callable[[str], str]) -> dict[str, QTreeWidgetItem]:
            """One section per group path, nested by "/" (parents created as needed)."""
            nodes: dict[str, QTreeWidgetItem] = {}

            def node(path: str) -> QTreeWidgetItem:
                if path not in nodes:
                    parent_path, _sep, leaf = path.rpartition(GROUP_SEP)
                    parent = node(parent_path) if parent_path else root
                    nodes[path] = section(title(leaf) if parent is not None else title(path), key_prefix + path, parent)
                return nodes[path]

            for path in paths:
                node(path)
            return nodes

        def host_item(parent: QTreeWidgetItem, host: SSHHost, kind: str | None = None) -> None:
            if kind is None:
                kind = KIND_GIT if host_role(host, settings.host_roles) is HostRole.GIT else KIND_HOST
            item = QTreeWidgetItem([host.alias])
            item.setData(0, ROLE_KIND, kind)
            item.setData(0, ROLE_ALIAS, host.alias)
            if kind == KIND_WILDCARD:
                item.setData(0, ROLE_SUBTITLE, "Defaults: " + (", ".join(f"{k} {v}" for k, v in host.to_options()[:2]) or "empty"))
                item.setToolTip(0, f"Host {' '.join(host.patterns)}\nThese options are defaults applied to every matching host.")
            elif kind == KIND_GIT:
                item.setData(0, ROLE_SUBTITLE, f"{host.display_address} · Git")
                tip = (f"{host.alias}\n{host.display_address}\n\nGit/service endpoint: SSH is used for authentication only "
                       "(no shell). Double-click tests the key.")
                if host.comment:
                    tip += f"\n\n{host.comment}"
                item.setToolTip(0, tip)
            else:
                item.setData(0, ROLE_SUBTITLE, host.display_address)
                tip = f"{host.alias}\n{host.display_address}"
                if host.proxy_jump:
                    tip += f"\nvia {host.proxy_jump}"
                if host.comment:
                    tip += f"\n\n{host.comment}"
                item.setToolTip(0, tip)
            team = teams_by_alias.get(host.alias)
            if team is not None:
                item.setData(0, ROLE_TEAM, team.name)
                item.setData(0, ROLE_TEAM_EDIT, team.can_edit)
                mine = self._my_login_of(host.alias)
                note = (f"You log in as {mine} (your own login, kept on this computer)." if mine
                        else "Your username is asked on first access." if not host.user else "")
                item.setToolTip(0, item.toolTip(0) + f"\n\nShared by team “{team.name}”." + (f"\n{note}" if note else ""))
            item.setData(0, ROLE_FAVORITE, settings.is_favorite(host.alias))
            group = settings.group_of(host.alias) or ""
            item.setData(0, ROLE_SEARCH, " ".join([host.alias, *host.aliases, host.hostname or "", host.user or "", group]).lower())
            parent.addChild(item)
            if host.alias == selected and kind == KIND_HOST and self.tree.currentItem() is None:
                self.tree.setCurrentItem(item)

        favorites = [by_alias[a] for a in settings.favorites if a in by_alias]
        if favorites:
            sec = section("★ " + SECTION_FAVORITES, SECTION_FAVORITES)
            for host in favorites:
                host_item(sec, host)
        recent = [by_alias[a] for a in settings.recent_connections if a in by_alias][:6]
        if recent:
            sec = section(SECTION_RECENT)
            for host in recent:
                host_item(sec, host)
        grouped: set[str] = set()
        # personal groups, nested: sub-groups first, then the hosts of each level
        nodes = group_tree(settings.group_paths(), "group:", None, str.upper)
        for group_name, sec in nodes.items():
            members = [by_alias[a] for a in settings.groups.get(group_name, []) if a in by_alias]
            for host in sorted(members, key=lambda h: h.alias.lower()):
                host_item(sec, host)
                grouped.add(host.alias)
        # team hosts: one section per team, with the team's (sub-)groups inside
        by_team: dict[str, tuple[TeamInfo, list[SSHHost]]] = {}
        for host in hosts:
            team = teams_by_alias.get(host.alias)
            if team is not None and host.alias not in grouped:
                by_team.setdefault(team.slug, (team, []))[1].append(host)
        for slug, (team, members) in sorted(by_team.items(), key=lambda kv: kv[1][0].name.lower()):
            root = section(f"TEAM · {team.name.upper()}", f"team:{slug}")
            path_of = {h.alias: normalize_group(team_group_of(h.alias)) for h in members}
            paths = sorted({p for p in path_of.values() if p}, key=lambda p: [x.lower() for x in p.split(GROUP_SEP)])
            team_nodes = group_tree(paths, f"team:{slug}:", root, str.upper)
            for host in sorted(members, key=lambda h: h.alias.lower()):  # grouped hosts go under their group
                if path_of[host.alias]:
                    host_item(team_nodes[path_of[host.alias]], host)
                grouped.add(host.alias)
            for host in sorted(members, key=lambda h: h.alias.lower()):  # then the team's ungrouped hosts
                if not path_of[host.alias]:
                    host_item(root, host)
        ungrouped = [h for h in hosts if h.alias not in grouped]
        servers = [h for h in ungrouped if host_role(h, settings.host_roles) is HostRole.SERVER]
        git_hosts = [h for h in ungrouped if host_role(h, settings.host_roles) is HostRole.GIT]
        if servers or not hosts:
            sec = section(SECTION_HOSTS)
            for host in servers:
                host_item(sec, host)
        if git_hosts:
            sec = section(SECTION_GIT)
            for host in git_hosts:
                host_item(sec, host)
        if defaults:
            sec = section(SECTION_DEFAULTS)
            for host in defaults:
                host_item(sec, host, KIND_WILDCARD)

        for item in self._sections():
            item.setExpanded(item.data(0, ROLE_SECTION) not in settings.collapsed_sections)
        self.empty_label.setVisible(not hosts)
        self._apply_filter(self.search.text())

    @staticmethod
    def _host_count(sec: QTreeWidgetItem) -> int:
        count = 0
        for j in range(sec.childCount()):
            child = sec.child(j)
            count += ConnectionPanel._host_count(child) if child.data(0, ROLE_KIND) == KIND_SECTION else 1
        return count

    def _section_state(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if item.data(0, ROLE_KIND) == KIND_SECTION and not self.search.text():
            self.section_toggled.emit(item.data(0, ROLE_SECTION), expanded)

    def _sections(self, parent: QTreeWidgetItem | None = None) -> list[QTreeWidgetItem]:
        """All section items, depth-first (sub-groups included)."""
        items = ([self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())] if parent is None
                 else [parent.child(i) for i in range(parent.childCount())])
        out: list[QTreeWidgetItem] = []
        for item in items:
            if item.data(0, ROLE_KIND) == KIND_SECTION:
                out.append(item)
                out.extend(self._sections(item))
        return out

    def _filter_section(self, sec: QTreeWidgetItem, query: str) -> int:
        """Hide non-matching rows below ``sec``; returns how many hosts stay visible."""
        visible = 0
        for j in range(sec.childCount()):
            child = sec.child(j)
            if child.data(0, ROLE_KIND) == KIND_SECTION:
                inner = self._filter_section(child, query)
                child.setHidden(bool(query) and inner == 0)
                visible += inner
                continue
            match = not query or query in (child.data(0, ROLE_SEARCH) or "")
            child.setHidden(not match)
            visible += int(match)
        return visible

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            sec = self.tree.topLevelItem(i)
            sec.setHidden(bool(query) and self._filter_section(sec, query) == 0)
        for sec in self._sections():
            if query:
                sec.setExpanded(True)
            else:
                sec.setExpanded(sec.data(0, ROLE_SECTION) not in self._settings.collapsed_sections)

    def _first_visible_host(self) -> str | None:
        def walk(parent: QTreeWidgetItem) -> str | None:
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.isHidden():
                    continue
                if child.data(0, ROLE_KIND) == KIND_SECTION:
                    found = walk(child)
                    if found:
                        return found
                elif child.data(0, ROLE_KIND) == KIND_HOST:
                    return child.data(0, ROLE_ALIAS)
            return None

        for i in range(self.tree.topLevelItemCount()):
            sec = self.tree.topLevelItem(i)
            if not sec.isHidden() and (found := walk(sec)):
                return found
        return None

    def _connect_first_match(self) -> None:
        alias = self._first_visible_host()
        if alias:
            self.connect_requested.emit(alias, True)

    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, ROLE_KIND) == KIND_SECTION:
            item.setExpanded(not item.isExpanded())

    def _double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        kind = item.data(0, ROLE_KIND)
        if kind == KIND_HOST:
            self.connect_requested.emit(item.data(0, ROLE_ALIAS), True)
        elif kind == KIND_GIT:
            self.git_test_requested.emit(item.data(0, ROLE_ALIAS))
        elif kind == KIND_WILDCARD:
            self.edit_defaults_requested.emit(item.data(0, ROLE_ALIAS))

    # ------------------------------------------------------------------ #
    def _context_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is None or item.data(0, ROLE_KIND) == KIND_SECTION:
            key = item.data(0, ROLE_SECTION) if item is not None else ""
            if isinstance(key, str) and key.startswith("team:"):
                slug = key.split(":")[1]
                menu.addAction("Restore Hosts Removed from My List", lambda s=slug: self.team_restore_requested.emit(s))
                menu.addSeparator()
            if isinstance(key, str) and key.startswith("group:"):
                group = key.removeprefix("group:")
                menu.addAction(icon("plus"), "New Sub-group…", lambda g=group: self.subgroup_requested.emit(g))
                menu.addAction("Rename Group…", lambda g=group: self.group_rename_requested.emit(g))
                menu.addAction(icon("x", current_palette().danger), "Delete Group (keeps the hosts)",
                               lambda g=group: self.group_delete_requested.emit(g))
                menu.addSeparator()
                if self._editable_teams:
                    count = self._host_count(item)
                    share = menu.addMenu(icon("server"), f"Share Group with Team ({count} host{'s' if count != 1 else ''})")
                    for team in self._editable_teams:
                        share.addAction(team.name, lambda t=team.id, g=group: self.share_group_requested.emit(g, t))
                    menu.addSeparator()
            else:
                menu.addAction(icon("plus"), "New Group…", lambda: self.subgroup_requested.emit(""))
            menu.addAction(icon("plus"), "New Connection", self.new_requested.emit)
            menu.addAction(icon("refresh"), "Reload SSH Config", self.reload_requested.emit)
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        alias = item.data(0, ROLE_ALIAS)
        selected = []
        for it in self.tree.selectedItems():
            a = it.data(0, ROLE_ALIAS)
            if it.data(0, ROLE_KIND) == KIND_HOST and a not in selected:
                selected.append(a)
        if len(selected) > 1 and item.isSelected():
            grid = menu.addAction(icon("grid"), f"Open {len(selected)} in Split View",
                                  lambda: self.split_view_requested.emit(selected))
            menu.setDefaultAction(grid)
            menu.addSeparator()
        if item.data(0, ROLE_KIND) == KIND_WILDCARD:
            menu.addAction("Edit SSH Defaults", lambda: self.edit_defaults_requested.emit(alias))
            menu.addAction(icon("file"), "Open Config", lambda: self.open_config_requested.emit(alias))
            menu.addSeparator()
            menu.addAction("Delete", lambda: self.delete_requested.emit(alias))
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        is_git = item.data(0, ROLE_KIND) == KIND_GIT
        if is_git:
            test = menu.addAction(icon("git"), "Test Git Authentication", lambda: self.git_test_requested.emit(alias))
            menu.setDefaultAction(test)
            menu.addAction("Copy Git Remote Example", lambda: self.copy_git_remote_requested.emit(alias))
            menu.addAction(icon("terminal"), "Open Terminal Anyway", lambda: self.connect_requested.emit(alias, True))
            menu.addAction(icon("server"), "Treat as SSH Server", lambda: self.role_set.emit(alias, HostRole.SERVER.value))
        else:
            connect = menu.addAction(icon("terminal"), "Connect", lambda: self.connect_requested.emit(alias, False))
            menu.setDefaultAction(connect)
            menu.addAction("Open in New Tab", lambda: self.connect_requested.emit(alias, True))
            menu.addAction(icon("split-right"), "Open to the Right (split)", lambda: self.open_split_requested.emit(alias, "right"))
            menu.addAction(icon("split-down"), "Open Below (split)", lambda: self.open_split_requested.emit(alias, "down"))
            files = menu.addMenu(icon("folder"), "Files (SFTP)")
            files.addAction("Open in New Tab (this computer | host)", lambda: self.files_requested.emit(alias, "tab"))
            files.addAction(icon("split-right"), "Open to the Right", lambda: self.files_requested.emit(alias, "right"))
            files.addAction(icon("split-down"), "Open Below", lambda: self.files_requested.emit(alias, "down"))
            menu.addAction("Test Connection", lambda: self.test_requested.emit(alias))
            menu.addAction(icon("git"), "Treat as Git / Service (no shell)", lambda: self.role_set.emit(alias, HostRole.GIT.value))
        menu.addSeparator()
        team_name = item.data(0, ROLE_TEAM)
        if team_name:
            if item.data(0, ROLE_TEAM_EDIT):
                menu.addAction(f"Edit in Team “{team_name}”…", lambda: self.team_edit_requested.emit(alias))
            else:
                managed = menu.addAction(f"Managed by team “{team_name}” (read-only)")
                managed.setEnabled(False)
            mine = self._my_login_of(alias)
            menu.addAction(f"My Login on This Host ({mine})…" if mine else "Set My Login…",
                           lambda: self.team_login_requested.emit(alias))
            if mine:
                menu.addAction("Use the Team's Login", lambda: self.team_login_reset_requested.emit(alias))
            menu.addAction("Duplicate as Personal Host", lambda: self.duplicate_requested.emit(alias))
        else:
            menu.addAction("Edit", lambda: self.edit_requested.emit(alias))
            menu.addAction("Duplicate", lambda: self.duplicate_requested.emit(alias))
            menu.addAction("Rename", lambda: self.rename_requested.emit(alias))
            if self._editable_teams:
                share = menu.addMenu("Share with Team")
                for team in self._editable_teams:
                    share.addAction(team.name, lambda t=team.id: self.share_requested.emit(alias, t))
        fav_text = "Remove from Favorites" if self._settings.is_favorite(alias) else "Add to Favorites"
        menu.addAction(icon("star"), fav_text, lambda: self.favorite_toggled.emit(alias))
        group_menu = menu.addMenu("Move to Group")
        current = self._settings.group_of(alias)
        for name in self._settings.group_paths():
            action = QAction(group_label(name), group_menu)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _c=False, n=name: self.group_set.emit(alias, n))
            group_menu.addAction(action)
        if self._settings.groups:
            group_menu.addSeparator()
        group_menu.addAction("New Group…  (Parent/Sub-group)", lambda: self.group_requested.emit(alias))
        if current:
            group_menu.addAction("Remove from Group", lambda: self.group_set.emit(alias, None))
        menu.addSeparator()
        menu.addAction("Copy SSH Command", lambda: self.copy_command_requested.emit(alias, False))
        menu.addAction("Copy Full SSH Command", lambda: self.copy_command_requested.emit(alias, True))
        menu.addAction(icon("file"), "Open Config", lambda: self.open_config_requested.emit(alias))
        menu.addSeparator()
        if team_name:
            menu.addAction(icon("x", current_palette().danger), "Remove from My List",
                           lambda: self.team_hide_requested.emit(alias))
            if item.data(0, ROLE_TEAM_EDIT):
                menu.addAction(icon("x", current_palette().danger), "Remove from Team (everyone)…",
                               lambda: self.team_delete_requested.emit(alias))
        else:
            menu.addAction(icon("x", current_palette().danger), "Delete", lambda: self.delete_requested.emit(alias))
        menu.exec(self.tree.viewport().mapToGlobal(pos))
