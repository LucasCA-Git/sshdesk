"""Release notes bundled with the app (``resources/CHANGELOG.md``)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ssh_terminal.utils.paths import resources_dir

_HEADER = re.compile(r"^##\s*\[?(?P<version>\d+(?:\.\d+)*)\]?\s*(?:-\s*(?P<date>\S+))?")


@dataclass
class Release:
    version: str
    date: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)

    def markdown(self) -> str:
        lines = [f"## {self.version}" + (f"  ·  {self.date}" if self.date else "")]
        for title, items in self.sections.items():
            lines.append(f"\n**{title}**\n")
            lines.extend(f"- {item}" for item in items)
        return "\n".join(lines)


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3]) or (0,)


def parse_changelog(text: str) -> list[Release]:
    releases: list[Release] = []
    section = "Changes"
    for raw in text.splitlines():
        line = raw.rstrip()
        if match := _HEADER.match(line):
            releases.append(Release(match["version"], match["date"] or ""))
            section = "Changes"
        elif not releases:
            continue
        elif line.startswith("### "):
            section = line[4:].strip()
        elif line.lstrip().startswith(("- ", "* ")):
            releases[-1].sections.setdefault(section, []).append(line.lstrip()[2:].strip())
    return sorted(releases, key=lambda r: version_key(r.version), reverse=True)


def load_releases(path: Path | None = None) -> list[Release]:
    try:
        return parse_changelog((path or resources_dir() / "CHANGELOG.md").read_text(encoding="utf-8"))
    except OSError:
        return []


def releases_since(releases: list[Release], previous: str, current: str) -> list[Release]:
    """Releases newer than ``previous`` up to ``current`` (only ``current`` when previous is unknown)."""
    cur = version_key(current)
    if not previous:
        return [r for r in releases if version_key(r.version) == cur][:1] or releases[:1]
    prev = version_key(previous)
    return [r for r in releases if prev < version_key(r.version) <= cur]
