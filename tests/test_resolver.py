"""Tests for effective-config resolution (built-in and `ssh -G`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ssh_terminal.models.ssh_host import SSHHost
from ssh_terminal.ssh.config_parser import SSHConfigSet
from ssh_terminal.ssh.resolver import (
    SSHConfigResolver,
    expand_tokens,
    host_matches,
    parse_ssh_g_output,
    resolve_jump_spec,
)


def test_host_matches() -> None:
    assert host_matches(["*"], "anything")
    assert host_matches(["*.example.com"], "hml.example.com")
    assert not host_matches(["*.example.com"], "example.org")
    assert not host_matches(["*", "!bastion"], "bastion")
    assert host_matches(["web-?"], "web-1")


def test_builtin_first_value_wins(ssh_config: Path) -> None:
    resolver = SSHConfigResolver(ssh_config, prefer_openssh=False)
    cfg = SSHConfigSet.load(ssh_config)
    prod = resolver.resolve_builtin("server-prod", cfg)
    assert (prod.hostname, prod.user, prod.port) == ("10.0.0.10", "lucas", 22)
    assert prod.server_alive_interval == 60  # inherited from Host *
    assert prod.identity_files == ["~/.ssh/id_ed25519"] and prod.identity_files_explicit


def test_builtin_inherits_wildcards(ssh_config: Path) -> None:
    resolver = SSHConfigResolver(ssh_config, prefer_openssh=False)
    cfg = SSHConfigSet.load(ssh_config)
    # "bastion" sets its own user; "foo.example.com" only matches the wildcards
    other = resolver.resolve_builtin("foo.example.com", cfg)
    assert other.user == "lucas"  # *.example.com comes before Host *
    unknown = resolver.resolve_builtin("10.9.9.9", cfg)
    assert unknown.user == "defaultuser" and unknown.hostname == "10.9.9.9"
    assert not unknown.identity_files_explicit


def test_builtin_alias_and_proxyjump(ssh_config: Path) -> None:
    resolver = SSHConfigResolver(ssh_config, prefer_openssh=False)
    cfg = SSHConfigSet.load(ssh_config)
    assert resolver.resolve_builtin("prod", cfg).hostname == "10.0.0.10"
    assert resolver.resolve_builtin("database", cfg).proxy_jump == "bastion"


def test_builtin_override_for_unsaved_host(ssh_config: Path) -> None:
    resolver = SSHConfigResolver(ssh_config, prefer_openssh=False)
    cfg = SSHConfigSet.load(ssh_config)
    edited = SSHHost(alias="server-prod", hostname="10.0.0.99", port=2200)
    r = resolver.resolve_builtin("server-prod", cfg, override=edited)
    assert (r.hostname, r.port, r.user) == ("10.0.0.99", 2200, "defaultuser")


def test_parse_ssh_g() -> None:
    out = """user deploy
hostname 10.0.0.50
port 2222
identityfile ~/.ssh/id_rsa
identityfile ~/.ssh/id_ed25519
proxyjump bastion
forwardagent yes
stricthostkeychecking false
serveraliveinterval 60
preferredauthentications publickey,password
userknownhostsfile ~/.ssh/known_hosts ~/.ssh/known_hosts2
"""
    r = parse_ssh_g_output("server-prod", out)
    assert r.endpoint == "deploy@10.0.0.50:2222"
    assert r.identity_files == ["~/.ssh/id_rsa", "~/.ssh/id_ed25519"]
    assert r.proxy_jump == "bastion"
    assert r.forward_agent is True
    assert r.strict_host_key_checking == "no"
    assert r.preferred_authentications == ["publickey", "password"]
    assert r.user_known_hosts_files == ["~/.ssh/known_hosts", "~/.ssh/known_hosts2"]
    assert r.source == "openssh"


def test_resolve_uses_ssh_g_when_available(ssh_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="hostname 1.2.3.4\nuser x\nport 22\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolver = SSHConfigResolver(ssh_config, prefer_openssh=True)
    resolver._ssh_binary = "/usr/bin/ssh"
    r = resolver.resolve("server-prod")
    assert r.source == "openssh" and r.hostname == "1.2.3.4"
    assert calls[0][-3:] == ["-G", "--", "server-prod"]
    assert r.identity_files_explicit  # IdentityFile set in the block


def test_resolve_falls_back_when_ssh_g_fails(ssh_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 255, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolver = SSHConfigResolver(ssh_config, prefer_openssh=True)
    resolver._ssh_binary = "/usr/bin/ssh"
    assert resolver.resolve("server-hml").source == "builtin"


def test_resolve_without_binary(ssh_config: Path) -> None:
    resolver = SSHConfigResolver(ssh_config, prefer_openssh=True)
    resolver._ssh_binary = None
    assert resolver.resolve("server-hml").port == 2222


def test_expand_tokens() -> None:
    assert expand_tokens("nc %h %p %%", hostname="h", port=22, user="u", alias="a") == "nc h 22 %"


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("bastion", (None, "bastion", None)),
        ("lucas@bastion:2222", ("lucas", "bastion", 2222)),
        ("ssh://u@[::1]:22", ("u", "::1", 22)),
    ],
)
def test_jump_spec(spec, expected) -> None:
    assert resolve_jump_spec(spec) == expected
