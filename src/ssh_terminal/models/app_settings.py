"""Application (UI) settings persisted in ``app_config.json``.

This file is UI metadata only. It never contains SSH connection parameters
(the SSH config is the source of truth) and never contains passwords.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from ssh_terminal.utils.platform import is_windows

DEFAULT_KEYBINDINGS: dict[str, str] = {
    "new_tab": "Ctrl+T",
    "close_tab": "Ctrl+W",
    "reopen_tab": "Ctrl+Shift+T",
    "next_tab": "Ctrl+Tab",
    "prev_tab": "Ctrl+Shift+Tab",
    "search": "Ctrl+Shift+F",
    "settings": "Ctrl+,",
    "copy": "Ctrl+Shift+C",
    "paste": "Ctrl+Shift+V",
    "split_right": "Ctrl+Shift+\\",
    "split_down": "Ctrl+Shift+-",
    "font_increase": "Ctrl+=",
    "font_decrease": "Ctrl+-",
    "font_reset": "Ctrl+0",
    "new_connection": "Ctrl+Shift+N",
    "reload_config": "F5",
    "fullscreen": "F11",
    "toggle_sidebar": "Ctrl+Shift+B",
}

KEYBINDING_LABELS: dict[str, str] = {
    "new_tab": "New tab (local terminal)",
    "close_tab": "Close tab / pane",
    "reopen_tab": "Reopen closed tab",
    "next_tab": "Next tab",
    "prev_tab": "Previous tab",
    "search": "Search connections",
    "settings": "Settings",
    "copy": "Copy",
    "paste": "Paste",
    "split_right": "Split right",
    "split_down": "Split down",
    "font_increase": "Increase font",
    "font_decrease": "Decrease font",
    "font_reset": "Reset font",
    "new_connection": "New connection",
    "reload_config": "Reload SSH config",
    "fullscreen": "Fullscreen",
    "toggle_sidebar": "Toggle sidebar",
}


def default_font_family() -> str:
    return "JetBrains Mono"


def font_fallbacks() -> list[str]:
    if is_windows():
        return ["JetBrains Mono", "Cascadia Mono", "Cascadia Code", "Consolas", "Courier New"]
    return ["JetBrains Mono", "DejaVu Sans Mono", "Liberation Mono", "Ubuntu Mono", "Noto Sans Mono", "Monospace"]


@dataclass
class AppSettings:
    # General
    ssh_config_path: str = ""  # empty = platform default (~/.ssh/config)
    first_run: bool = True
    confirm_close_connected: bool = True
    watch_config: bool = True
    auto_reload_config: bool = False  # reload silently instead of asking
    restore_tabs: bool = False
    default_local_shell: str = ""  # ShellProfile.name; empty = platform default
    # Appearance
    theme: str = "dark"  # dark | light | system
    color_scheme: str = "Default Dark"
    window_opacity: int = 100  # percent
    # Terminal
    font_family: str = field(default_factory=default_font_family)
    font_size: int = 13
    cursor_style: str = "block"  # block | underline | bar
    cursor_blink: bool = True
    scrollback_lines: int = 10000
    copy_on_select: bool = False
    paste_on_right_click: bool = False
    bell: str = "visual"  # none | visual | sound
    term_type: str = "xterm-256color"
    # SSH
    use_openssh_resolver: bool = True
    connect_timeout: int = 15
    keepalive_interval: int = 30
    auto_reconnect: bool = False
    reconnect_attempts: int = 5
    use_agent: bool = True
    apply_config_forwards: bool = True
    # Security
    allow_keyring: bool = True
    default_strict_host_key_checking: str = "ask"
    backup_count: int = 20
    # Keyboard
    keybindings: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYBINDINGS))
    # UI metadata
    sidebar_width: int = 280
    sidebar_visible: bool = True
    window_geometry: str = ""  # base64 QByteArray
    favorites: list[str] = field(default_factory=list)
    recent_connections: list[str] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)
    host_roles: dict[str, str] = field(default_factory=dict)
    # Team sharing (account/token are kept by TeamService; the token is in the OS keyring)
    team_server_url: str = "http://localhost:8080"
    team_sync_minutes: int = 5  # alias -> "server" | "git" (manual override)
    collapsed_sections: list[str] = field(default_factory=list)
    open_tabs: list[dict[str, Any]] = field(default_factory=list)

    MAX_RECENT = 10

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        """Tolerant loader: unknown keys ignored, wrong types replaced by defaults."""
        defaults = cls()
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            default_value = getattr(defaults, f.name)
            if isinstance(default_value, bool):
                if isinstance(value, bool):
                    kwargs[f.name] = value
            elif isinstance(default_value, int):
                if isinstance(value, int) and not isinstance(value, bool):
                    kwargs[f.name] = value
            elif isinstance(default_value, type(value)):
                kwargs[f.name] = value
        settings = cls(**kwargs)
        merged = dict(DEFAULT_KEYBINDINGS)
        merged.update({k: v for k, v in settings.keybindings.items() if k in DEFAULT_KEYBINDINGS and isinstance(v, str)})
        settings.keybindings = merged
        settings.favorites = [x for x in settings.favorites if isinstance(x, str)]
        settings.recent_connections = [x for x in settings.recent_connections if isinstance(x, str)][: cls.MAX_RECENT]
        settings.groups = {
            str(name): [a for a in members if isinstance(a, str)]
            for name, members in settings.groups.items()
            if isinstance(members, list)
        }
        return settings

    # ------------------------------------------------------------------ #
    def is_favorite(self, alias: str) -> bool:
        return alias in self.favorites

    def toggle_favorite(self, alias: str) -> bool:
        if alias in self.favorites:
            self.favorites.remove(alias)
            return False
        self.favorites.append(alias)
        return True

    def add_recent(self, alias: str) -> None:
        if alias in self.recent_connections:
            self.recent_connections.remove(alias)
        self.recent_connections.insert(0, alias)
        del self.recent_connections[self.MAX_RECENT :]

    def group_of(self, alias: str) -> str | None:
        for name, members in self.groups.items():
            if alias in members:
                return name
        return None

    def set_group(self, alias: str, group: str | None) -> None:
        for members in self.groups.values():
            if alias in members:
                members.remove(alias)
        if group:
            self.groups.setdefault(group, []).append(alias)
        self.groups = {k: v for k, v in self.groups.items() if v or k == group}

    def rename_alias(self, old: str, new: str) -> None:
        """Keep metadata attached to a host across renames."""
        if old in self.host_roles:
            self.host_roles[new] = self.host_roles.pop(old)
        self.favorites = [new if a == old else a for a in self.favorites]
        self.recent_connections = [new if a == old else a for a in self.recent_connections]
        for members in self.groups.values():
            for i, a in enumerate(members):
                if a == old:
                    members[i] = new

    def forget_alias(self, alias: str) -> None:
        self.host_roles.pop(alias, None)
        self.favorites = [a for a in self.favorites if a != alias]
        self.recent_connections = [a for a in self.recent_connections if a != alias]
        for members in self.groups.values():
            while alias in members:
                members.remove(alias)
