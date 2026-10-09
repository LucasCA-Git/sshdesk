"""Tests for plain data models."""

from __future__ import annotations

import pytest

from ssh_terminal.models.app_settings import DEFAULT_KEYBINDINGS, AppSettings
from ssh_terminal.models.connection import ConnectionKind, ConnectionSpec, PortForwardSpec
from ssh_terminal.models.ssh_host import SSHHost, is_pattern, parse_bool


def test_host_defaults() -> None:
    h = SSHHost(alias="srv")
    assert h.effective_port == 22
    assert h.target_host == "srv"
    assert h.display_address == "srv"


def test_display_address() -> None:
    h = SSHHost(alias="x", hostname="10.0.0.1", user="lucas", port=2222)
    assert h.display_address == "lucas@10.0.0.1:2222"


def test_search() -> None:
    h = SSHHost(alias="api-prod", hostname="10.1.1.1", user="deploy")
    assert h.matches_search("PROD")
    assert h.matches_search("10.1")
    assert h.matches_search("depl")
    assert h.matches_search("infra", group="Infrastructure")
    assert not h.matches_search("hml")


def test_to_options_roundtrip() -> None:
    h = SSHHost(alias="a", hostname="h", user="u", port=22, identity_files=["k1", "k2"], forward_agent=True,
                extra_options=[("SendEnv", "LANG"), ("SendEnv", "LC_*")])
    rebuilt = SSHHost.from_options(h.patterns, h.to_options())
    assert rebuilt == h


def test_is_pattern() -> None:
    assert is_pattern("*") and is_pattern("web-?") and is_pattern("!bad")
    assert not is_pattern("server-prod")


@pytest.mark.parametrize("value,expected", [("yes", True), ("No", False), ("confirm", True), (None, None)])
def test_parse_bool(value, expected) -> None:
    assert parse_bool(value) is expected


def test_clone() -> None:
    h = SSHHost(alias="a", aliases=["b"], identity_files=["k"])
    c = h.clone("a-copy")
    c.identity_files.append("x")
    assert c.alias == "a-copy" and c.aliases == []
    assert h.identity_files == ["k"]


def test_settings_roundtrip_and_tolerance() -> None:
    s = AppSettings()
    s.toggle_favorite("server-prod")
    s.add_recent("server-hml")
    data = s.to_dict()
    assert "password" not in str(data).lower()
    loaded = AppSettings.from_dict(data)
    assert loaded.favorites == ["server-prod"]
    assert loaded.recent_connections == ["server-hml"]
    broken = AppSettings.from_dict({"font_size": "big", "theme": 3, "unknown": 1, "keybindings": {"new_tab": "Alt+N"}})
    assert broken.font_size == AppSettings().font_size
    assert broken.theme == "dark"
    assert broken.keybindings["new_tab"] == "Alt+N"
    assert broken.keybindings["close_tab"] == DEFAULT_KEYBINDINGS["close_tab"]


def test_recent_limit_and_order() -> None:
    s = AppSettings()
    for i in range(15):
        s.add_recent(f"h{i}")
    s.add_recent("h5")
    assert s.recent_connections[0] == "h5"
    assert len(s.recent_connections) == AppSettings.MAX_RECENT


def test_groups_and_rename() -> None:
    s = AppSettings()
    s.set_group("server-prod", "Production")
    s.set_group("api-prod", "Production")
    s.set_group("server-prod", "Infra")
    assert s.groups == {"Production": ["api-prod"], "Infra": ["server-prod"]}
    s.toggle_favorite("server-prod")
    s.rename_alias("server-prod", "prod")
    assert s.group_of("prod") == "Infra" and s.favorites == ["prod"]
    s.forget_alias("prod")
    assert s.group_of("prod") is None and s.favorites == []


def test_connection_spec_roundtrip() -> None:
    spec = ConnectionSpec.ssh("db")
    assert ConnectionSpec.from_dict(spec.to_dict()) == spec
    assert ConnectionSpec.local("bash").kind is ConnectionKind.LOCAL


@pytest.mark.parametrize(
    "kind,spec,expected",
    [
        ("local", "8080 localhost:80", ("127.0.0.1", 8080, "localhost", 80)),
        ("local", "0.0.0.0:5432 db.internal:5432", ("0.0.0.0", 5432, "db.internal", 5432)),
        ("remote", "9000 127.0.0.1:3000", ("127.0.0.1", 9000, "127.0.0.1", 3000)),
        ("local", "[::1]:8080 [fe80::1]:80", ("::1", 8080, "fe80::1", 80)),
        ("dynamic", "1080", ("127.0.0.1", 1080, "", 0)),
        ("dynamic", "*:1080", ("0.0.0.0", 1080, "", 0)),
    ],
)
def test_port_forward_parse(kind, spec, expected) -> None:
    f = PortForwardSpec.parse(kind, spec)
    assert (f.bind_address, f.bind_port, f.target_host, f.target_port) == expected


def test_port_forward_invalid() -> None:
    with pytest.raises(ValueError):
        PortForwardSpec.parse("local", "abc")


def test_subgroups() -> None:
    from ssh_terminal.models.app_settings import AppSettings, group_label, normalize_group

    assert normalize_group(" Prod /  Web  EU / ") == "Prod/Web EU"
    assert group_label("Prod/Web") == "Prod › Web"
    s = AppSettings()
    s.set_group("web-1", "Prod/Web")
    s.set_group("db-1", "Prod")
    s.add_group("Prod/Web/EU")
    assert s.group_paths() == ["Prod", "Prod/Web", "Prod/Web/EU"]
    assert s.subgroups("Prod") == ["Prod/Web", "Prod/Web/EU"]
    assert s.group_of("web-1") == "Prod/Web"
    # leaving a group that still has sub-groups keeps it
    s.set_group("web-1", "Prod/Web/EU")
    assert "Prod/Web" in s.groups and s.groups["Prod/Web/EU"] == ["web-1"]
    # renaming moves the whole branch
    assert s.rename_group("Prod", "Production") == "Production"
    assert s.group_of("web-1") == "Production/Web/EU" and s.group_of("db-1") == "Production"
    assert s.rename_group("Production", "Production/Inside") == "Production"  # not inside itself
    # deleting removes the branch, hosts become ungrouped
    assert sorted(s.delete_group("Production/Web")) == ["web-1"]
    assert s.group_of("web-1") is None and s.group_paths() == ["Production"]


def test_group_names_normalised_on_load() -> None:
    from ssh_terminal.models.app_settings import AppSettings

    s = AppSettings.from_dict({"groups": {"Prod / EU": ["a"], "Prod/EU": ["a", "b"], " / ": ["c"], "x": "bad"}})
    assert s.groups == {"Prod/EU": ["a", "b"]}
