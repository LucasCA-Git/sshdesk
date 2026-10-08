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
            "Default Light", "#2b2f36", "#fafafa", "#2b2f36", "#c9d7f2",
            ("#2b2f36", "#c4314b", "#3b8a3b", "#a06a00", "#2a62c9", "#9a3fb5", "#14869a", "#d0d4da",
             "#6b7280", "#e0475f", "#4ea24e", "#c08400", "#3b78e7", "#b456d1", "#1ba3ba", "#ffffff"),
            dark=False,
        ),
    ]
}

DEFAULT_SCHEME = "Default Dark"


def get_scheme(name: str) -> ColorScheme:
    return SCHEMES.get(name) or SCHEMES[DEFAULT_SCHEME]
