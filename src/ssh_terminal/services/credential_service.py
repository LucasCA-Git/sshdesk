"""Secure credential storage through the OS keyring.

* Windows: Windows Credential Manager
* Linux:   Secret Service (GNOME Keyring / KWallet) via D-Bus
* macOS:   Keychain

If no system keyring exists (common on WSL, servers and minimal Linux
desktops) secrets go to an encrypted local vault instead
(``<app dir>/credentials.vault``, Fernet/AES with a random key in
``<app dir>/vault.key``, both readable only by the user). That protects the
passwords at rest the same way an unencrypted ``~/.ssh/id_*`` key is
protected: by file permissions.

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


class EncryptedFileVault:
    """Keyring-compatible store in an encrypted file (fallback when no system keyring)."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.key_path = directory / "vault.key"
        self.data_path = directory / "credentials.vault"
        self._lock = threading.Lock()

    def _fernet(self):  # noqa: ANN202 - cryptography.fernet.Fernet
        from cryptography.fernet import Fernet

        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
            _restrict(self.key_path)
        return Fernet(self.key_path.read_bytes().strip())

    def _load(self) -> dict[str, str]:
        if not self.data_path.exists():
            return {}
        from cryptography.fernet import InvalidToken

        try:
            data = json.loads(self._fernet().decrypt(self.data_path.read_bytes()))
            return data if isinstance(data, dict) else {}
        except (InvalidToken, ValueError, OSError) as exc:
            log.warning("Credential vault unreadable (%s); starting empty", type(exc).__name__)
            return {}

    def _save(self, data: dict[str, str]) -> None:
        token = self._fernet().encrypt(json.dumps(data).encode())
        tmp = self.data_path.with_suffix(".tmp")
        tmp.write_bytes(token)
        _restrict(tmp)
        tmp.replace(self.data_path)

    def get_password(self, service: str, name: str) -> str | None:
        with self._lock:
            return self._load().get(f"{service}/{name}")

    def set_password(self, service: str, name: str, secret: str) -> None:
        with self._lock:
            data = self._load()
            data[f"{service}/{name}"] = secret
            self._save(data)

    def delete_password(self, service: str, name: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(f"{service}/{name}", None) is not None:
                self._save(data)


def _restrict(path: Path) -> None:
    """Owner-only permissions (POSIX); on Windows %APPDATA% is already per-user."""
    import os

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class KeyringCredentialStore:
    def __init__(self, index_path: Path | None = None) -> None:
        self.index_path = index_path or (get_app_dir() / "credentials_index.json")
        self._lock = threading.Lock()
        self._available: bool | None = None
        self._keyring = None
        self._vault = EncryptedFileVault(self.index_path.parent)

    @property
    def available(self) -> bool:
        """Secrets can always be saved: system keyring, or the encrypted vault."""
        return True

    @property
    def system_keyring(self) -> bool:
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
        if not self.system_keyring or self._keyring is None:
            return "Encrypted local file (no system keyring found)"
        return type(self._keyring.get_keyring()).__name__

    def _store(self):  # noqa: ANN202 - keyring module or EncryptedFileVault (same API)
        return self._keyring if self.system_keyring else self._vault

    # ------------------------------------------------------------------ #
    @staticmethod
    def _password_key(user: str, host: str, port: int) -> str:
        return f"password:{user}@{host}:{port}"

    @staticmethod
    def _passphrase_key(key_path: str) -> str:
        return f"passphrase:{key_path}"

    def _get(self, name: str) -> str | None:
        try:
            value = self._store().get_password(SERVICE_NAME, name)
        except Exception as exc:  # noqa: BLE001 - keyring backends raise various errors
            log.warning("Keyring read failed: %s", type(exc).__name__)
            value = None
        if value is None and self.system_keyring:
            try:  # saved earlier while the system keyring was locked/unavailable
                value = self._vault.get_password(SERVICE_NAME, name)
            except Exception:  # noqa: BLE001
                value = None
        return value

    def _set(self, name: str, secret: str) -> None:
        where = "keyring" if self.system_keyring else "vault"
        try:
            self._store().set_password(SERVICE_NAME, name, secret)
        except Exception as exc:  # noqa: BLE001 - e.g. locked keyring in a headless session
            log.warning("Keyring write failed (%s); using encrypted vault", type(exc).__name__)
            try:
                self._vault.set_password(SERVICE_NAME, name, secret)
                where = "vault"
            except Exception as exc2:  # noqa: BLE001
                log.warning("Vault write failed: %s", type(exc2).__name__)
                return
        self._index_add(name)
        log.info("Credential stored (%s, %s)", name.split(":", 1)[0], where)

    def _delete(self, name: str) -> None:
        stores = [self._store()] + ([self._vault] if self.system_keyring else [])
        for store in stores:
            try:
                store.delete_password(SERVICE_NAME, name)
            except Exception as exc:  # noqa: BLE001 - PasswordDeleteError when absent
                log.debug("Credential delete: %s", type(exc).__name__)
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
