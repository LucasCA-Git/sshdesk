"""Secure credential storage through the OS keyring.

* Windows: Windows Credential Manager
* Linux:   Secret Service (GNOME Keyring / KWallet) via D-Bus
* macOS:   Keychain

Secrets are never written to ``app_config.json``. A small index of the
*names* of stored entries (no secrets) is kept so "Clear saved credentials"
can remove them all, because keyring backends cannot enumerate entries.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ssh_terminal.utils.paths import get_app_dir

log = logging.getLogger(__name__)

SERVICE_NAME = "SSHDesk"


class KeyringCredentialStore:
    def __init__(self, index_path: Path | None = None) -> None:
        self.index_path = index_path or (get_app_dir() / "credentials_index.json")
        self._lock = threading.Lock()
        self._available: bool | None = None
        self._keyring = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                import keyring
                from keyring.backends import fail

                backend = keyring.get_keyring()
                self._available = not isinstance(backend, fail.Keyring)
                self._keyring = keyring
                log.info("Keyring backend: %s (available=%s)", type(backend).__name__, self._available)
            except Exception as exc:  # noqa: BLE001 - any import/backend error means "unavailable"
                log.warning("Keyring unavailable: %s", exc)
                self._available = False
        return bool(self._available)

    def backend_name(self) -> str:
        if not self.available or self._keyring is None:
            return "unavailable"
        return type(self._keyring.get_keyring()).__name__

    # ------------------------------------------------------------------ #
    @staticmethod
    def _password_key(user: str, host: str, port: int) -> str:
        return f"password:{user}@{host}:{port}"

    @staticmethod
    def _passphrase_key(key_path: str) -> str:
        return f"passphrase:{key_path}"

    def _get(self, name: str) -> str | None:
        if not self.available:
            return None
        try:
            return self._keyring.get_password(SERVICE_NAME, name)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - keyring backends raise various errors
            log.warning("Keyring read failed: %s", type(exc).__name__)
            return None

    def _set(self, name: str, secret: str) -> None:
        if not self.available:
            return
        try:
            self._keyring.set_password(SERVICE_NAME, name, secret)  # type: ignore[union-attr]
            self._index_add(name)
            log.info("Credential stored in keyring (%s)", name.split(":", 1)[0])
        except Exception as exc:  # noqa: BLE001
            log.warning("Keyring write failed: %s", type(exc).__name__)

    def _delete(self, name: str) -> None:
        if not self.available:
            return
        try:
            self._keyring.delete_password(SERVICE_NAME, name)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - PasswordDeleteError when absent
            log.debug("Keyring delete: %s", type(exc).__name__)
        self._index_remove(name)

    # ------------------------------------------------------------------ #
    def get_password(self, user: str, host: str, port: int) -> str | None:
        return self._get(self._password_key(user, host, port))

    def set_password(self, user: str, host: str, port: int, password: str) -> None:
        self._set(self._password_key(user, host, port), password)

    def delete_password(self, user: str, host: str, port: int) -> None:
        self._delete(self._password_key(user, host, port))

    def get_passphrase(self, key_path: str) -> str | None:
        return self._get(self._passphrase_key(key_path))

    def set_passphrase(self, key_path: str, passphrase: str) -> None:
        self._set(self._passphrase_key(key_path), passphrase)

    def delete_passphrase(self, key_path: str) -> None:
        self._delete(self._passphrase_key(key_path))

    def stored_entries(self) -> list[str]:
        return self._index_read()

    def clear_all(self) -> int:
        names = self._index_read()
        for name in names:
            self._delete(name)
        return len(names)

    # ------------------------------------------------------------------ #
    def _index_read(self) -> list[str]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return [x for x in data if isinstance(x, str)] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _index_write(self, names: list[str]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(sorted(set(names)), indent=2), encoding="utf-8")

    def _index_add(self, name: str) -> None:
        with self._lock:
            names = self._index_read()
            if name not in names:
                self._index_write([*names, name])

    def _index_remove(self, name: str) -> None:
        with self._lock:
            names = self._index_read()
            if name in names:
                self._index_write([n for n in names if n != name])
