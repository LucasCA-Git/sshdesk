"""Duplicate detection between personal and team hosts."""

from __future__ import annotations

from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.services.host_identity import dedupe, find_duplicate, same_server


def h(alias: str, hostname: str = "", user: str | None = None, port: int | None = None, jump: str | None = None) -> SSHHost:
    return SSHHost(alias=alias, hostname=hostname or None, user=user, port=port, proxy_jump=jump)


def test_same_server_rules() -> None:
    base = h("a", "10.0.0.10", "deploy")
    assert same_server(base, h("b", "10.0.0.10", "deploy", 22))
    assert same_server(base, h("b", "10.0.0.10"))  # user unset: same login
    assert same_server(base, {"alias": "t", "hostname": "10.0.0.10", "user": "deploy", "port": 22})
    assert not same_server(base, h("b", "10.0.0.10", "root"))
    assert not same_server(base, h("b", "10.0.0.10", "deploy", 2222))
    assert not same_server(base, h("b", "10.0.0.10", "deploy", jump="bastion"))  # other network
    assert same_server(h("x"), h("y", "x"))  # no HostName -> the alias is the host


def test_find_duplicate_by_name_case_insensitive_then_server() -> None:
    hosts = [h("Web-Prod", "10.0.0.1"), h("db", "10.0.0.2", "postgres")]
    assert find_duplicate(h("web-prod", "1.1.1.1"), hosts)[0] == "name"
    kind, other = find_duplicate(h("database", "10.0.0.2"), hosts)
    assert kind == "server" and other.alias == "db"
    assert find_duplicate(h("new", "10.0.0.3"), hosts) is None
    assert find_duplicate(h("db", "10.0.0.2"), hosts, ignore_aliases=["db"]) is None  # editing itself


def test_dedupe_prefers_personal_hosts() -> None:
    personal = [h("web", "10.0.0.1"), h("web", "10.0.0.99")]  # repeated block: OpenSSH uses the first
    team = [h("web-team", "10.0.0.1"), h("WEB", "10.0.0.5"), h("api", "10.0.0.7")]
    result = dedupe(team + personal, prefer=personal)
    assert [(x.alias, x.hostname) for x in result] == [("api", "10.0.0.7"), ("web", "10.0.0.1")]
