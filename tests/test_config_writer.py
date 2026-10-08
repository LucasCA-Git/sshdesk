"""Tests for surgical edits and atomic writing of the SSH config."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from conftest import SAMPLE_CONFIG
from ssh_terminal.errors import DuplicateHostError, HostNotFoundError
from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.ssh.config_parser import SSHConfigDocument
from ssh_terminal.ssh.config_writer import ConfigFileWriter, add_host, remove_host, update_host


def _doc() -> SSHConfigDocument:
    return SSHConfigDocument.parse(SAMPLE_CONFIG)


def _get(doc: SSHConfigDocument, alias: str) -> SSHHost:
    return next(h for h in doc.hosts() if h.alias == alias)


def test_noop_update_keeps_file_identical() -> None:
    doc = _doc()
    for host in doc.hosts():
        if not host.is_wildcard:
            update_host(doc, host.alias, host)
    assert doc.render() == SAMPLE_CONFIG


def test_change_port_only_changes_one_line() -> None:
    doc = _doc()
    host = _get(doc, "server-prod")
    host.port = 2222
    update_host(doc, "server-prod", host)
    before = SAMPLE_CONFIG.splitlines()
    after = doc.render().splitlines()
    diff = [(a, b) for a, b in zip(before, after) if a != b]
    assert diff == [("    Port 22", "    Port 2222")]
    assert len(before) == len(after)


def test_unknown_option_survives_edit() -> None:
    doc = _doc()
    host = _get(doc, "server")
    host.user = "root"
    update_host(doc, "server", host)
    text = doc.render()
    assert "    CustomOption something" in text
    assert "    User root" in text


def test_add_new_option_appended_inside_block() -> None:
    doc = _doc()
    host = _get(doc, "bastion")
    host.port = 2200
    host.forward_agent = True
    update_host(doc, "bastion", host)
    text = doc.render()
    block = text.split("Host bastion")[1].split("Host database")[0]
    assert "    Port 2200\n" in block
    assert "    ForwardAgent yes\n" in block
    assert block.endswith("\n\n")  # blank separator kept after the new lines


def test_remove_option() -> None:
    doc = _doc()
    host = _get(doc, "server-hml")
    host.identity_files = []
    update_host(doc, "server-hml", host)
    assert "id_rsa" not in doc.render()


def test_rename_host_keeps_aliases_and_options() -> None:
    doc = _doc()
    host = _get(doc, "server-prod")
    host.alias = "production"
    update_host(doc, "server-prod", host)
    text = doc.render()
    assert "Host production prod\n" in text
    assert "Host server-prod" not in text


def test_rename_to_existing_alias_fails() -> None:
    doc = _doc()
    host = _get(doc, "server-prod")
    host.alias = "bastion"
    with pytest.raises(DuplicateHostError):
        update_host(doc, "server-prod", host)


def test_add_host_goes_before_catch_all() -> None:
    doc = _doc()
    add_host(doc, SSHHost(alias="production-server", hostname="10.0.0.10", user="lucas", port=22, identity_files=["~/.ssh/id_ed25519"]))
    text = doc.render()
    expected = (
        "Host production-server\n"
        "    HostName 10.0.0.10\n"
        "    User lucas\n"
        "    Port 22\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
    )
    assert expected in text
    assert text.index("Host production-server") < text.index("Host *\n")
    assert SSHConfigDocument.parse(text).render() == text


def test_add_host_to_empty_file() -> None:
    doc = SSHConfigDocument.parse("")
    add_host(doc, SSHHost(alias="a", hostname="1.1.1.1"))
    assert doc.render() == "Host a\n    HostName 1.1.1.1\n"


def test_add_host_appends_with_blank_separator() -> None:
    doc = SSHConfigDocument.parse("Host a\n    HostName x")
    add_host(doc, SSHHost(alias="b", hostname="y"))
    assert doc.render() == "Host a\n    HostName x\n\nHost b\n    HostName y\n"


def test_add_duplicate_fails() -> None:
    with pytest.raises(DuplicateHostError):
        add_host(_doc(), SSHHost(alias="bastion"))


def test_remove_host_with_its_comment() -> None:
    doc = _doc()
    remove_host(doc, "bastion")
    text = doc.render()
    assert "bastion.example.com" not in text
    assert "# Jump box" not in text
    assert "Host database" in text
    assert "\n\n\n" not in text


def test_remove_unknown_host() -> None:
    with pytest.raises(HostNotFoundError):
        remove_host(_doc(), "nope")


def test_paths_with_spaces_are_quoted() -> None:
    doc = SSHConfigDocument.parse("")
    add_host(doc, SSHHost(alias="w", identity_files=["C:\\Users\\A B\\.ssh\\id"]))
    assert 'IdentityFile "C:\\Users\\A B\\.ssh\\id"' in doc.render()
    assert SSHConfigDocument.parse(doc.render()).hosts()[0].identity_files == ["C:\\Users\\A B\\.ssh\\id"]


def test_crlf_preserved_on_edit() -> None:
    text = SAMPLE_CONFIG.replace("\n", "\r\n")
    doc = SSHConfigDocument.parse(text)
    host = _get(doc, "server-prod")
    host.user = "root"
    update_host(doc, "server-prod", host)
    out = doc.render()
    assert "\n" not in out.replace("\r\n", "")


# ---------------------------------------------------------------------- #
def test_writer_backup_and_atomic(ssh_config: Path, tmp_path: Path) -> None:
    writer = ConfigFileWriter(tmp_path / "backups", keep=3)
    writer.write(ssh_config, "Host new\n")
    assert ssh_config.read_text() == "Host new\n"
    assert (ssh_config.parent / "config.bak").read_text() == SAMPLE_CONFIG
    assert len(writer.list_backups()) == 1
    leftovers = [p for p in ssh_config.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_writer_prunes_backups(ssh_config: Path, tmp_path: Path) -> None:
    writer = ConfigFileWriter(tmp_path / "backups", keep=2)
    for i in range(5):
        writer.write(ssh_config, f"Host h{i}\n")
    assert len(writer.list_backups()) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_writer_keeps_permissions(ssh_config: Path, tmp_path: Path) -> None:
    os.chmod(ssh_config, 0o640)
    ConfigFileWriter(tmp_path / "b").write(ssh_config, "Host x\n")
    assert stat.S_IMODE(ssh_config.stat().st_mode) == 0o640


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_writer_new_file_is_private(isolated_home: Path, tmp_path: Path) -> None:
    path = isolated_home / ".ssh" / "config"
    ConfigFileWriter(tmp_path / "b").write(path, "Host x\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_writer_follows_symlink(isolated_home: Path, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles_config"
    real.write_text("Host a\n")
    link = isolated_home / ".ssh" / "config"
    link.symlink_to(real)
    ConfigFileWriter(tmp_path / "b").write(link, "Host b\n")
    assert link.is_symlink()
    assert real.read_text() == "Host b\n"
