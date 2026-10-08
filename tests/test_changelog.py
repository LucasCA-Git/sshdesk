"""Bundled release notes."""

from __future__ import annotations

from ssh_terminal import __version__
from ssh_terminal.services.changelog import load_releases, parse_changelog, releases_since, version_key

SAMPLE = """# Changelog
intro line
## [1.10.0] - 2027-01-02
### New
- big thing
### Fixed
- bug
## [1.2.0] - 2026-11-01
- loose item
## [1.1.0]
### New
- thing
"""


def test_parse_and_order() -> None:
    releases = parse_changelog(SAMPLE)
    assert [r.version for r in releases] == ["1.10.0", "1.2.0", "1.1.0"]  # numeric, not text order
    assert releases[0].sections == {"New": ["big thing"], "Fixed": ["bug"]}
    assert releases[1].sections == {"Changes": ["loose item"]}
    assert "**Fixed**" in releases[0].markdown() and "2027-01-02" in releases[0].markdown()


def test_releases_since() -> None:
    releases = parse_changelog(SAMPLE)
    assert [r.version for r in releases_since(releases, "1.1.0", "1.10.0")] == ["1.10.0", "1.2.0"]
    assert [r.version for r in releases_since(releases, "", "1.2.0")] == ["1.2.0"]  # unknown previous
    assert releases_since(releases, "1.10.0", "1.10.0") == []
    assert version_key("1.10.0") > version_key("1.9.9")


def test_bundled_changelog_has_current_version() -> None:
    releases = load_releases()
    assert releases and releases[0].version == __version__, "add a CHANGELOG entry when bumping __version__"
    assert all(r.sections for r in releases)


def test_root_changelog_and_package_version_in_sync() -> None:
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    bundled = root / "src" / "ssh_terminal" / "resources" / "CHANGELOG.md"
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == bundled.read_text(encoding="utf-8"), \
        "copy src/ssh_terminal/resources/CHANGELOG.md to the repository root"
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__, "bump pyproject.toml together with __version__"
