"""When are two host entries "the same host"?

Used to keep the host list free of duplicates across the personal SSH config
and team hosts:

* **Same name**: aliases compared case-insensitively (OpenSSH lower-cases the
  destination before matching ``Host`` patterns, so ``Web`` and ``web`` clash).
* **Same server**: same HostName, port and ProxyJump, and the same user, or
  one of the two leaves the user unset (it then comes from ``Host *`` or the
  local user). ProxyJump is part of the identity because private addresses
  such as ``10.0.0.10`` are reused behind different bastions.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ssh_terminal.models.ssh_host import SSHHost


def alias_key(alias: str) -> str:
    return alias.strip().lower()


def endpoint(host: SSHHost | dict[str, Any]) -> tuple[str, int, str, str]:
    """(hostname, port, user, proxy_jump) of a host model or a team host payload."""
    if isinstance(host, SSHHost):
        return (
            (host.target_host or "").strip().lower(),
            int(host.effective_port),
            (host.user or "").strip(),
            (host.proxy_jump or "").strip().lower(),
        )
    return (
        str(host.get("hostname") or host.get("alias") or "").strip().lower(),
        int(host.get("port") or 22),
        str(host.get("user") or "").strip(),
        str(host.get("proxy_jump") or "").strip().lower(),
    )


def same_server(a: SSHHost | dict[str, Any], b: SSHHost | dict[str, Any]) -> bool:
    ha, pa, ua, ja = endpoint(a)
    hb, pb, ub, jb = endpoint(b)
    if not ha or ha != hb or pa != pb or ja != jb:
        return False
    return ua == ub or not ua or not ub


def alias_of(host: SSHHost | dict[str, Any]) -> str:
    return host.alias if isinstance(host, SSHHost) else str(host.get("alias", ""))


def find_duplicate(host: SSHHost | dict[str, Any], others: Iterable[SSHHost | dict[str, Any]],
                   ignore_aliases: Iterable[str] = ()) -> tuple[str, SSHHost | dict[str, Any]] | None:
    """First entry of ``others`` that duplicates ``host``: ("name" | "server", entry)."""
    ignored = {alias_key(a) for a in ignore_aliases}
    key = alias_key(alias_of(host))
    candidates = [o for o in others if alias_key(alias_of(o)) not in ignored]
    for other in candidates:
        if alias_key(alias_of(other)) == key:
            return "name", other
    for other in candidates:
        if same_server(host, other):
            return "server", other
    return None


def dedupe(hosts: list[SSHHost], prefer: Iterable[SSHHost] = ()) -> list[SSHHost]:
    """Drop repeated hosts, keeping the first occurrence; hosts in ``prefer`` are kept over the rest.

    Repeated names are always dropped (OpenSSH only ever uses the first block).
    Same-server duplicates are dropped only when they are not in ``prefer``
    (e.g. a team host pointing at a server you already have personally).
    """
    preferred = {id(h) for h in prefer}
    ordered = sorted(hosts, key=lambda h: id(h) not in preferred)  # stable: preferred first
    seen_names: set[str] = set()
    kept: list[SSHHost] = []
    for host in ordered:
        name = alias_key(host.alias)
        if name in seen_names:
            continue
        if id(host) not in preferred and any(same_server(host, k) for k in kept if id(k) in preferred):
            continue
        seen_names.add(name)
        kept.append(host)
    order = {id(h): i for i, h in enumerate(hosts)}
    return sorted(kept, key=lambda h: order[id(h)])
