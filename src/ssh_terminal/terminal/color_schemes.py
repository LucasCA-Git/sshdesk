"""Terminal colour schemes.

A scheme is 16 ANSI colours + foreground/background/cursor/selection. Adding
a scheme = adding one :class:`ColorScheme` entry to :data:`SCHEMES`.
"""

from __future__ import annotations

from dataclasses import dataclass

ANSI_NAMES = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]


@dataclass(frozen=True)
class ColorScheme:
    name: str
    foreground: str
    background: str
    cursor: str
    selection: str
    ansi: tuple[str, ...]  # 16 colours: 0-7 normal, 8-15 bright
    dark: bool = True
    accent: str = ""  # highlight for the active pane border / previews (defaults to green)

    @property
    def highlight(self) -> str:
        return self.accent or self.ansi[10]

    def color_for(self, name: str, default: str) -> str:
        """Map a pyte colour (name, ``default`` or 6-digit hex) to ``#rrggbb``."""
        if name == "default":
            return default
        if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
            return "#" + name
        bright = name.startswith("bright")
        base = name[6:] if bright else name
        if base in ANSI_NAMES:
            return self.ansi[ANSI_NAMES.index(base) + (8 if bright else 0)]
        return default

    def brighten(self, name: str) -> str:
        """Bold text in a normal ANSI colour is drawn with the bright variant."""
        if name in ANSI_NAMES:
            return "bright" + name
        return name


SCHEMES: dict[str, ColorScheme] = {
    s.name: s
    for s in [
        ColorScheme(
            "Default Dark", "#d4d7dd", "#14161b", "#e6e6e6", "#3a4a66",
            ("#1d1f24", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#c8ccd4",
             "#5c6370", "#ff7a85", "#b5e890", "#ffd58f", "#7cc4ff", "#de95f5", "#6fd4e0", "#ffffff"),
        ),
        ColorScheme(
            "Dracula", "#f8f8f2", "#282a36", "#f8f8f2", "#44475a",
            ("#21222c", "#ff5555", "#50fa7b", "#f1fa8c", "#bd93f9", "#ff79c6", "#8be9fd", "#f8f8f2",
             "#6272a4", "#ff6e6e", "#69ff94", "#ffffa5", "#d6acff", "#ff92df", "#a4ffff", "#ffffff"),
        ),
        ColorScheme(
            "Solarized Dark", "#839496", "#002b36", "#93a1a1", "#073642",
            ("#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#eee8d5",
             "#002b36", "#cb4b16", "#586e75", "#657b83", "#839496", "#6c71c4", "#93a1a1", "#fdf6e3"),
        ),
        ColorScheme(
            "Monokai", "#f8f8f2", "#272822", "#f8f8f0", "#49483e",
            ("#272822", "#f92672", "#a6e22e", "#f4bf75", "#66d9ef", "#ae81ff", "#a1efe4", "#f8f8f2",
             "#75715e", "#f92672", "#a6e22e", "#f4bf75", "#66d9ef", "#ae81ff", "#a1efe4", "#f9f8f5"),
        ),
        ColorScheme(
            "Nord", "#d8dee9", "#2e3440", "#d8dee9", "#434c5e",
            ("#3b4252", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead", "#88c0d0", "#e5e9f0",
             "#4c566a", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead", "#8fbcbb", "#eceff4"),
        ),
        ColorScheme(
            "Gruvbox", "#ebdbb2", "#282828", "#ebdbb2", "#504945",
            ("#282828", "#cc241d", "#98971a", "#d79921", "#458588", "#b16286", "#689d6a", "#a89984",
             "#928374", "#fb4934", "#b8bb26", "#fabd2f", "#83a598", "#d3869b", "#8ec07c", "#ebdbb2"),
        ),
        ColorScheme(
            "Midnight", "#c9d4e0", "#0e1621", "#3ddc97", "#22384a",
            ("#16202c", "#f07178", "#3ddc97", "#f2c66d", "#5ab0f6", "#c792ea", "#4fd6be", "#c9d4e0",
             "#4b5d73", "#ff8b92", "#6bf0b4", "#ffd88a", "#82c6ff", "#ddb2ff", "#7fe8d6", "#ffffff"),
            accent="#3ddc97",
        ),
        ColorScheme(
            "Kanagawa Wave", "#dcd7ba", "#1f1f28", "#c8c093", "#2d4f67",
            ("#090618", "#c34043", "#76946a", "#c0a36e", "#7e9cd8", "#957fb8", "#6a9589", "#c8c093",
             "#727169", "#e82424", "#98bb6c", "#e6c384", "#7fb4ca", "#938aa9", "#7aa89f", "#dcd7ba"),
            accent="#7e9cd8",
        ),
        ColorScheme(
            "Kanagawa Dragon", "#c5c9c5", "#181616", "#c8c093", "#2d4f67",
            ("#0d0c0c", "#c4746e", "#8a9a7b", "#c4b28a", "#8ba4b0", "#a292a3", "#8ea4a2", "#c8c093",
             "#a6a69c", "#e46876", "#87a987", "#e6c384", "#7fb4ca", "#938aa9", "#7aa89f", "#c5c9c5"),
            accent="#c4b28a",
        ),
        ColorScheme(
            "Kanagawa Lotus", "#545464", "#f2ecbc", "#43436c", "#c9cbd1",
            ("#1f1f28", "#c84053", "#6f894e", "#77713f", "#4d699b", "#b35b79", "#597b75", "#545464",
             "#8a8980", "#d7474b", "#6e915f", "#836f4a", "#6693bf", "#624c83", "#5e857a", "#43436c"),
            dark=False,
            accent="#4d699b",
        ),
        ColorScheme(
            "Everforest Dark", "#d3c6aa", "#2d353b", "#d3c6aa", "#475258",
            ("#343f44", "#e67e80", "#a7c080", "#dbbc7f", "#7fbbb3", "#d699b6", "#83c092", "#d3c6aa",
             "#859289", "#e67e80", "#a7c080", "#dbbc7f", "#7fbbb3", "#d699b6", "#83c092", "#e4e1cd"),
            accent="#a7c080",
        ),
        ColorScheme(
            "Everforest Light", "#5c6a72", "#fdf6e3", "#5c6a72", "#e6e2cc",
            ("#5c6a72", "#f85552", "#8da101", "#dfa000", "#3a94c5", "#df69ba", "#35a77c", "#dfddc8",
             "#829181", "#f85552", "#8da101", "#dfa000", "#3a94c5", "#df69ba", "#35a77c", "#a6b0a0"),
            dark=False,
            accent="#8da101",
        ),
        ColorScheme(
            "Hacker Green", "#39ff88", "#050b07", "#39ff88", "#123a22",
            ("#050b07", "#ff5f5f", "#28d470", "#b8ff5c", "#3fbf7f", "#5fd7a7", "#2ee6a6", "#a8ffc8",
             "#1f6b3c", "#ff8787", "#39ff88", "#d4ff8a", "#5fe0a0", "#87ffc0", "#5fffd0", "#e0ffe9"),
            accent="#39ff88",
        ),
        ColorScheme(
            "Hacker Blue", "#4fb8ff", "#040912", "#4fb8ff", "#12304d",
            ("#040912", "#ff6b6b", "#3fd0c9", "#ffd166", "#3a8fff", "#8f7bff", "#2fd3ff", "#b8dcff",
             "#1d4470", "#ff8f8f", "#6fe3dc", "#ffe08f", "#6aabff", "#ae9eff", "#6fe2ff", "#e6f3ff"),
            accent="#4fb8ff",
        ),
        ColorScheme(
            "Hacker Red", "#ff4d5a", "#100406", "#ff4d5a", "#4a1218",
            ("#100406", "#ff3344", "#ff8a5c", "#ffb347", "#ff5c8a", "#ff4dc4", "#ff7a7a", "#ffc2c7",
             "#6e1a22", "#ff6675", "#ffa47e", "#ffc56e", "#ff80a6", "#ff7ad3", "#ff9e9e", "#ffe6e8"),
            accent="#ff4d5a",
        ),
        ColorScheme(
            "Default Light", "#2b2f36", "#fafafa", "#2b2f36", "#c9d7f2",
            ("#2b2f36", "#c4314b", "#3b8a3b", "#a06a00", "#2a62c9", "#9a3fb5", "#14869a", "#d0d4da",
             "#6b7280", "#e0475f", "#4ea24e", "#c08400", "#3b78e7", "#b456d1", "#1ba3ba", "#ffffff"),
            dark=False,
        ),
    ]
}

DEFAULT_SCHEME = "Midnight"


def get_scheme(name: str) -> ColorScheme:
    return SCHEMES.get(name) or SCHEMES["Default Dark"]
