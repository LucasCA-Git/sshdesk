"""Server vs Git/service endpoint classification."""

from __future__ import annotations

import pytest

from ssh_terminal.models.app_settings import AppSettings
from ssh_terminal.ssh.config_parser import SSHConfigDocument
from ssh_terminal.ssh.host_classifier import HostRole, git_remote_example, guess_role, host_role

# The exact config from the bug report: one real server, two Git identities.
CONFIG = """Host painel
    HostName 192.168.10.20
    User lucas.cardoso
    IdentityFile ~/.ssh/lucas.cardoso
    IdentitiesOnly yes

Host lucas.cardoso
    HostName github.com
    User git
    IdentityFile ~/.ssh/lucas.cardoso
    IdentitiesOnly yes

# GitHub pessoal
Host github-pessoal
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_github_pessoal
    IdentitiesOnly yes
"""


def _hosts():
    return {h.alias: h for h in SSHConfigDocument.parse(CONFIG).hosts()}


def test_report_config() -> None:
    hosts = _hosts()
    assert guess_role(hosts["painel"]) is HostRole.SERVER
    assert guess_role(hosts["lucas.cardoso"]) is HostRole.GIT
    assert guess_role(hosts["github-pessoal"]) is HostRole.GIT


@pytest.mark.parametrize(
    "block,role",
    [
        ("Host gl\n  HostName gitlab.com\n", HostRole.GIT),  # known provider, no User
        ("Host bb\n  HostName bitbucket.org\n  User someone\n", HostRole.GIT),
        ("Host az\n  HostName ssh.dev.azure.com\n  User git\n", HostRole.GIT),
        ("Host corp-gitlab\n  HostName gitlab.empresa.local\n  User git\n", HostRole.GIT),  # self-hosted
        ("Host github.com\n  User git\n", HostRole.GIT),  # alias is the hostname
        ("Host web\n  HostName 10.0.0.5\n  User deploy\n", HostRole.SERVER),
        ("Host gitserver\n  HostName git.empresa.local\n  User admin\n", HostRole.SERVER),  # admin shell
    ],
)
def test_guess(block: str, role: HostRole) -> None:
    assert guess_role(SSHConfigDocument.parse(block).hosts()[0]) is role


def test_override_wins_and_follows_rename() -> None:
    hosts = _hosts()
    settings = AppSettings()
    settings.host_roles["lucas.cardoso"] = "server"
    assert host_role(hosts["lucas.cardoso"], settings.host_roles) is HostRole.SERVER
    settings.rename_alias("lucas.cardoso", "gh-work")
    assert settings.host_roles == {"gh-work": "server"}
    settings.forget_alias("gh-work")
    assert settings.host_roles == {}
    assert AppSettings.from_dict({"host_roles": {"a": "git"}}).host_roles == {"a": "git"}


def test_git_remote_example() -> None:
    assert git_remote_example(_hosts()["github-pessoal"]) == "git@github-pessoal:<owner>/<repo>.git"
