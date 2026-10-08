"""Tests for the lossless SSH config parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SAMPLE_CONFIG
from ssh_terminal.ssh.config_parser import BlockKind, SSHConfigDocument, SSHConfigSet, split_arguments


def test_roundtrip_is_byte_identical() -> None:
    doc = SSHConfigDocument.parse(SAMPLE_CONFIG)
    assert doc.render() == SAMPLE_CONFIG


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Host a\n",
        "Host a",  # no trailing newline
        "Host a\r\n  HostName x\r\n",  # CRLF
        "\ufeffHost a\n\tUser b\n",  # BOM + tabs
        "Host a\n    HostName=x\n    Port = 22\n\n\n# end\n",
        "   \n# only comments\n\n",
        "Host a\nweird line with ### stuff !!\n",
    ],
)
def test_roundtrip_edge_cases(text: str) -> None:
    assert SSHConfigDocument.parse(text).render() == text


def test_parse_host() -> None:
    doc = SSHConfigDocument.parse(SAMPLE_CONFIG)
    hosts = {h.alias: h for h in doc.hosts()}
    prod = hosts["server-prod"]
    assert prod.hostname == "10.0.0.10"
    assert prod.user == "lucas"
    assert prod.port == 22
    assert prod.identity_files == ["~/.ssh/id_ed25519"]
    assert prod.aliases == ["prod"]
    assert hosts["server-hml"].port == 2222
    assert hosts["database"].proxy_jump == "bastion"
    assert hosts["bastion"].port is None
    assert hosts["bastion"].effective_port == 22


def test_unknown_options_are_kept() -> None:
    doc = SSHConfigDocument.parse(SAMPLE_CONFIG)
    server = next(h for h in doc.hosts() if h.alias == "server")
    assert server.extra_options == [("CustomOption", "something")]


def test_wildcards_are_flagged() -> None:
    doc = SSHConfigDocument.parse(SAMPLE_CONFIG)
    wild = [h.alias for h in doc.hosts() if h.is_wildcard]
    concrete = [h.alias for h in doc.hosts() if not h.is_wildcard]
    assert wild == ["*.example.com", "*"]
    assert "*" not in concrete


def test_match_blocks_are_not_hosts() -> None:
    doc = SSHConfigDocument.parse(SAMPLE_CONFIG)
    kinds = [b.kind for b in doc.blocks]
    assert BlockKind.MATCH in kinds
    assert all(h.alias != "host" for h in doc.hosts())


def test_equals_syntax_and_case_insensitive_keywords() -> None:
    doc = SSHConfigDocument.parse("HOST x\n  hostname=1.2.3.4\n  PORT = 2200\n  forwardagent Yes\n")
    host = doc.hosts()[0]
    assert host.hostname == "1.2.3.4"
    assert host.port == 2200
    assert host.forward_agent is True


def test_quoted_values() -> None:
    doc = SSHConfigDocument.parse('Host "my host" other\n  IdentityFile "C:\\Users\\A B\\.ssh\\id"\n')
    host = doc.hosts()[0]
    assert host.alias == "my host"
    assert host.identity_files == ["C:\\Users\\A B\\.ssh\\id"]


def test_split_arguments() -> None:
    assert split_arguments('a "b c" d') == ["a", "b c", "d"]
    assert split_arguments('""') == [""]
    assert split_arguments("  ") == []


def test_all_supported_fields() -> None:
    text = """Host full
    HostName h
    User u
    Port 2022
    IdentityFile ~/.ssh/a
    IdentityFile ~/.ssh/b
    ProxyJump j1,j2
    ProxyCommand ssh -W %h:%p jump
    ForwardAgent yes
    ForwardX11 no
    Compression yes
    ServerAliveInterval 30
    ServerAliveCountMax 4
    StrictHostKeyChecking accept-new
    PreferredAuthentications publickey,password
    IdentitiesOnly yes
    AddKeysToAgent yes
    LocalForward 8080 localhost:80
"""
    h = SSHConfigDocument.parse(text).hosts()[0]
    assert h.identity_files == ["~/.ssh/a", "~/.ssh/b"]
    assert h.proxy_jump == "j1,j2"
    assert h.proxy_command == "ssh -W %h:%p jump"
    assert h.forward_agent is True and h.forward_x11 is False and h.compression is True
    assert h.server_alive_interval == 30 and h.server_alive_count_max == 4
    assert h.strict_host_key_checking == "accept-new"
    assert h.preferred_authentications == "publickey,password"
    assert h.identities_only is True
    assert h.add_keys_to_agent == "yes"
    assert h.local_forwards == ["8080 localhost:80"]
    assert h.extra_options == []


def test_comments_glued_to_host() -> None:
    doc = SSHConfigDocument.parse(SAMPLE_CONFIG)
    bastion = doc.find_block("bastion")
    assert bastion is not None
    assert [ln.raw for ln in bastion.leading] == ["# Jump box"]


def test_include_files(ssh_config: Path) -> None:
    inc_dir = ssh_config.parent / "config.d"
    inc_dir.mkdir()
    (inc_dir / "work").write_text("Host work-vm\n    HostName 192.168.0.5\n", encoding="utf-8")
    cfg = SSHConfigSet.load(ssh_config)
    aliases = [h.alias for h in cfg.concrete_hosts()]
    assert "work-vm" in aliases
    found = cfg.find("work-vm")
    assert found is not None and found[0].path == inc_dir / "work"


def test_missing_file_gives_empty_doc(tmp_path: Path) -> None:
    doc = SSHConfigDocument.load(tmp_path / "nope")
    assert doc.hosts() == []
    assert doc.render() == ""
