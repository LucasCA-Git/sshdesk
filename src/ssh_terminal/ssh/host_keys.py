"""known_hosts handling.

We read the user's and system's ``known_hosts`` files and only ever *append*
new entries (never rewrite the file), so hashed entries, comments and
``@cert-authority`` lines written by OpenSSH are left untouched.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import paramiko

from ssh_terminal.utils.platform import is_windows

log = logging.getLogger(__name__)

# Host key algorithm names paramiko can negotiate for each key type stored in
# known_hosts. Used to prefer an algorithm we already trust (like OpenSSH).
KEYTYPE_ALGORITHMS: dict[str, list[str]] = {
    "ssh-ed25519": ["ssh-ed25519"],
    "ssh-rsa": ["rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"],
    "ecdsa-sha2-nistp256": ["ecdsa-sha2-nistp256"],
    "ecdsa-sha2-nistp384": ["ecdsa-sha2-nistp384"],
    "ecdsa-sha2-nistp521": ["ecdsa-sha2-nistp521"],
}


class HostKeyStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"


@dataclass
class HostKeyCheck:
    status: HostKeyStatus
    host_id: str
    key_type: str
    fingerprint: str


def fingerprint_sha256(key: paramiko.PKey) -> str:
    """OpenSSH style ``SHA256:...`` fingerprint."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def host_id_for(hostname: str, port: int) -> str:
    """known_hosts host column: ``host`` for port 22, ``[host]:port`` otherwise."""
    return hostname if port == 22 else f"[{hostname}]:{port}"


class KnownHostsStore:
    def __init__(self, user_files: list[Path], global_files: list[Path] | None = None) -> None:
        self.user_files = user_files or [Path.home() / ".ssh" / "known_hosts"]
        self.global_files = global_files or []
        self.host_keys = paramiko.HostKeys()
        for path in [*self.user_files, *self.global_files]:
            self._load(path)

    def _load(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Cannot read %s: %s", path, exc)
            return
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("@"):
                continue  # markers (@cert-authority/@revoked) are not supported by paramiko
            try:
                entry = paramiko.hostkeys.HostKeyEntry.from_line(line, lineno)
            except (paramiko.SSHException, ValueError) as exc:
                log.debug("Skipping invalid known_hosts line %s:%d (%s)", path, lineno, exc)
                continue
            if entry is None or entry.key is None:
                continue
            for name in entry.hostnames:
                self.host_keys.add(name, entry.key.get_name(), entry.key)

    def known_key_types(self, host_id: str) -> list[str]:
        found = self.host_keys.lookup(host_id)
        return list(found.keys()) if found else []

    def preferred_algorithms(self, host_id: str, available: tuple[str, ...]) -> tuple[str, ...]:
        """Reorder ``available`` so algorithms matching already-known keys come first."""
        preferred: list[str] = []
        for key_type in self.known_key_types(host_id):
            for alg in KEYTYPE_ALGORITHMS.get(key_type, [key_type]):
                if alg in available and alg not in preferred:
                    preferred.append(alg)
        return tuple(preferred + [a for a in available if a not in preferred])

    def check(self, host_id: str, key: paramiko.PKey) -> HostKeyCheck:
        key_type = key.get_name()
        fp = fingerprint_sha256(key)
        found = self.host_keys.lookup(host_id)
        if not found:
            return HostKeyCheck(HostKeyStatus.UNKNOWN, host_id, key_type, fp)
        known = found.get(key_type)
        if known is None:
            # RSA keys may be negotiated with rsa-sha2-*; paramiko stores them as ssh-rsa.
            known = found.get(key.get_name())
        if known is None:
            return HostKeyCheck(HostKeyStatus.UNKNOWN, host_id, key_type, fp)
        if known.asbytes() == key.asbytes():
            return HostKeyCheck(HostKeyStatus.KNOWN, host_id, key_type, fp)
        return HostKeyCheck(HostKeyStatus.MISMATCH, host_id, key_type, fp)

    def add(self, host_id: str, key: paramiko.PKey) -> Path:
        """Append ``host_id key`` to the first user known_hosts file."""
        path = self.user_files[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        prefix = ""
        if not new_file:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                if fh.tell() > 0:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        prefix = "\n"
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{prefix}{host_id} {key.get_name()} {key.get_base64()}\n")
        if new_file and not is_windows():
            os.chmod(path, 0o600)
        self.host_keys.add(host_id, key.get_name(), key)
        log.info("Added host key for %s to %s", host_id, path)
        return path
