"""Tell interactive SSH servers apart from Git/service endpoints.

Many ``~/.ssh/config`` files contain entries that exist only so Git picks the
right key, e.g.::

    Host github-pessoal
        HostName github.com
        User git
        IdentityFile ~/.ssh/id_github_pessoal

Those hosts authenticate over SSH but never give you a shell, so opening a
terminal tab for them is pointless. They are shown in their own sidebar
section and double-click tests the authentication instead.

The guess can be overridden per host (stored in app_config.json, never in the
SSH config).
"""

from __future__ import annotations

from enum import StrEnum

from ssh_terminal.models.ssh_host import SSHHost


class HostRole(StrEnum):
    SERVER = "server"
    GIT = "git"


# Hosts that only serve Git over SSH (no interactive shell).
GIT_HOSTNAMES = {
    "github.com",
    "ssh.github.com",
    "gitlab.com",
    "altssh.gitlab.com",
    "bitbucket.org",
    "altssh.bitbucket.org",
    "ssh.dev.azure.com",
    "vs-ssh.visualstudio.com",
    "codeberg.org",
    "git.sr.ht",
    "gitea.com",
    "aur.archlinux.org",
    "heroku.com",
    "source.developers.google.com",
}
GIT_HOSTNAME_SUFFIXES = (".ghe.com", ".visualstudio.com")
# Conventional service accounts used by Git servers (incl. self-hosted GitLab/Gitea).
GIT_USERS = {"git", "aur", "hg", "gitea", "forgejo"}


def guess_role(host: SSHHost) -> HostRole:
    """Best-effort classification from the SSH config alone."""
    user = (host.user or "").strip().lower()
    hostname = (host.hostname or host.alias).strip().lower()
    if user in GIT_USERS:
        return HostRole.GIT
    if hostname in GIT_HOSTNAMES or hostname.endswith(GIT_HOSTNAME_SUFFIXES):
        return HostRole.GIT
    return HostRole.SERVER


def host_role(host: SSHHost, overrides: dict[str, str] | None = None) -> HostRole:
    """Role of a host, honouring a user override if there is one."""
    if overrides:
        value = overrides.get(host.alias)
        if value in (HostRole.SERVER.value, HostRole.GIT.value):
            return HostRole(value)
    return guess_role(host)


def git_remote_example(host: SSHHost) -> str:
    """How to use this alias in a Git remote URL."""
    return f"git@{host.alias}:<owner>/<repo>.git" if (host.user or "git") == "git" else f"{host.user}@{host.alias}:<repo>.git"
