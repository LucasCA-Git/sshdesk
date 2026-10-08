"""SSH user authentication (public key, agent, keyboard-interactive, password).

Authentication runs in a worker thread. Whenever the user must be asked
something (password, passphrase, host key, 2FA code) the worker calls an
:class:`AuthPrompter`; the Qt implementation marshals the question to the GUI
thread and blocks the worker until it is answered.

Secrets are never logged and never written to disk; when the user opts in,
they go to the OS keyring through :class:`CredentialStore`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import paramiko

from ssh_terminal.errors import AuthenticationFailed, ConnectionCancelled
from ssh_terminal.ssh.resolver import ResolvedConfig
from ssh_terminal.utils.paths import expand_user_path

log = logging.getLogger(__name__)

SUPPORTED_METHODS = ("publickey", "keyboard-interactive", "password")
MAX_PASSWORD_ATTEMPTS = 3


@dataclass
class SecretAnswer:
    value: str
    remember: bool = False


class AuthPrompter(Protocol):
    """Questions the SSH layer may need to ask the user."""

    def ask_password(self, prompt: str, can_remember: bool) -> SecretAnswer | None: ...

    def ask_passphrase(self, key_path: str, can_remember: bool) -> SecretAnswer | None: ...

    def confirm_host_key(self, host: str, key_type: str, fingerprint: str) -> bool: ...

    def keyboard_interactive(
        self, title: str, instructions: str, prompts: list[tuple[str, bool]]
    ) -> list[str] | None: ...


class NonInteractivePrompter:
    """Prompter that never asks anything (CLI / tests). Unknown host keys are rejected."""

    def __init__(self, accept_new_host_keys: bool = False) -> None:
        self.accept_new_host_keys = accept_new_host_keys

    def ask_password(self, prompt: str, can_remember: bool) -> SecretAnswer | None:
        return None

    def ask_passphrase(self, key_path: str, can_remember: bool) -> SecretAnswer | None:
        return None

    def confirm_host_key(self, host: str, key_type: str, fingerprint: str) -> bool:
        return self.accept_new_host_keys

    def keyboard_interactive(self, title: str, instructions: str, prompts: list[tuple[str, bool]]) -> list[str] | None:
        return None


class CredentialStore(Protocol):
    """Secure secret storage (implemented with ``keyring``)."""

    def get_password(self, user: str, host: str, port: int) -> str | None: ...

    def set_password(self, user: str, host: str, port: int, password: str) -> None: ...

    def delete_password(self, user: str, host: str, port: int) -> None: ...

    def get_passphrase(self, key_path: str) -> str | None: ...

    def set_passphrase(self, key_path: str, passphrase: str) -> None: ...

    def delete_passphrase(self, key_path: str) -> None: ...

    @property
    def available(self) -> bool: ...


class NullCredentialStore:
    available = False

    def get_password(self, user: str, host: str, port: int) -> str | None:
        return None

    def set_password(self, user: str, host: str, port: int, password: str) -> None:
        return None

    def delete_password(self, user: str, host: str, port: int) -> None:
        return None

    def get_passphrase(self, key_path: str) -> str | None:
        return None

    def set_passphrase(self, key_path: str, passphrase: str) -> None:
        return None

    def delete_passphrase(self, key_path: str) -> None:
        return None


@dataclass
class AuthOptions:
    use_agent: bool = True
    allow_keyring: bool = True
    password: str | None = None  # one-shot password typed in a dialog (never persisted here)


@dataclass
class AuthResult:
    method: str
    tried: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- #
# Keys
# ---------------------------------------------------------------------- #
def _public_blob(path: Path) -> bytes | None:
    """Public key blob from ``<key>.pub`` (lets us skip keys the agent already has)."""
    pub = path.with_name(path.name + ".pub")
    try:
        parts = pub.read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(parts) < 2:
        return None
    import base64
    import binascii

    try:
        return base64.b64decode(parts[1])
    except (binascii.Error, ValueError):
        return None


def load_private_key(
    path: Path,
    prompter: AuthPrompter,
    credentials: CredentialStore,
    allow_keyring: bool = True,
) -> paramiko.PKey | None:
    """Load a private key, asking for (and optionally remembering) its passphrase."""
    try:
        key = paramiko.PKey.from_path(path)
    except paramiko.PasswordRequiredException:
        key = None
    except TypeError as exc:
        # For the OpenSSH key format the "encrypted" signal comes from
        # `cryptography` as a TypeError ("Key is password-protected...").
        if "password" not in str(exc).lower():
            log.warning("Cannot load key %s: %s", path, exc)
            return None
        key = None
    except (paramiko.SSHException, ValueError, OSError, UnicodeDecodeError) as exc:
        log.warning("Cannot load key %s: %s", path, exc)
        return None

    if key is None:
        stored = credentials.get_passphrase(str(path)) if allow_keyring and credentials.available else None
        if stored:
            try:
                key = paramiko.PKey.from_path(path, password=stored.encode())
            except (paramiko.SSHException, ValueError, TypeError):
                log.info("Stored passphrase for %s is no longer valid", path)
                credentials.delete_passphrase(str(path))
        attempts = 0
        while key is None and attempts < MAX_PASSWORD_ATTEMPTS:
            attempts += 1
            answer = prompter.ask_passphrase(str(path), allow_keyring and credentials.available)
            if answer is None:
                return None
            try:
                key = paramiko.PKey.from_path(path, password=answer.value.encode())
            except (paramiko.SSHException, ValueError, TypeError):
                log.info("Wrong passphrase for %s", path)
                continue
            if answer.remember and allow_keyring and credentials.available:
                credentials.set_passphrase(str(path), answer.value)
    if key is None:
        return None

    cert = path.with_name(path.name + "-cert.pub")
    if cert.is_file():
        try:
            key.load_certificate(str(cert))
        except (paramiko.SSHException, ValueError, OSError) as exc:
            log.warning("Ignoring certificate %s: %s", cert, exc)
    return key


# ---------------------------------------------------------------------- #
# Authenticator
# ---------------------------------------------------------------------- #
class Authenticator:
    def __init__(
        self,
        prompter: AuthPrompter,
        credentials: CredentialStore,
        options: AuthOptions | None = None,
        agent_factory: Callable[[], paramiko.Agent] | None = None,
    ) -> None:
        self.prompter = prompter
        self.credentials = credentials
        self.options = options or AuthOptions()
        self._agent_factory = agent_factory or paramiko.Agent
        self._password_cache: str | None = self.options.password

    # ------------------------------------------------------------------ #
    def authenticate(self, transport: paramiko.Transport, cfg: ResolvedConfig) -> AuthResult:
        user = cfg.user
        tried: list[str] = []
        try:
            transport.auth_none(user)
            if transport.is_authenticated():
                return AuthResult("none", tried)
            allowed = list(SUPPORTED_METHODS)
        except paramiko.BadAuthenticationType as exc:
            allowed = list(exc.allowed_types)
        except paramiko.AuthenticationException:
            allowed = list(SUPPORTED_METHODS)

        order = [m for m in cfg.preferred_authentications if m in SUPPORTED_METHODS] or list(SUPPORTED_METHODS)

        for method in order:
            if transport.is_authenticated():
                break
            if method not in allowed:
                continue
            tried.append(method)
            if method == "publickey":
                remaining = self._publickey(transport, cfg)
            elif method == "keyboard-interactive":
                remaining = self._keyboard_interactive(transport, cfg)
            else:
                remaining = self._password(transport, cfg)
            if remaining is not None and not transport.is_authenticated():
                allowed = remaining  # partial success (e.g. key + OTP)

        if transport.is_authenticated():
            log.info("Authenticated to %s as %s (%s)", cfg.hostname, user, tried[-1] if tried else "none")
            return AuthResult(tried[-1] if tried else "none", tried)
        raise AuthenticationFailed("Authentication failed", tried, allowed)

    # ------------------------------------------------------------------ #
    def _agent_keys(self) -> tuple[paramiko.Agent | None, list[paramiko.AgentKey]]:
        if not self.options.use_agent:
            return None, []
        try:
            agent = self._agent_factory()
            return agent, list(agent.get_keys())
        except (paramiko.SSHException, OSError) as exc:
            log.info("SSH agent unavailable: %s", exc)
            return None, []

    def _identity_paths(self, cfg: ResolvedConfig) -> list[Path]:
        paths: list[Path] = []
        for raw in cfg.identity_files:
            path = expand_user_path(raw)
            if path.is_file() and path not in paths:
                paths.append(path)
            elif cfg.identity_files_explicit and not path.is_file():
                log.warning("IdentityFile not found: %s", path)
        return paths

    def _publickey(self, transport: paramiko.Transport, cfg: ResolvedConfig) -> list[str] | None:
        agent, agent_keys = self._agent_keys()
        try:
            identity_paths = self._identity_paths(cfg)
            identity_blobs = {b for b in (_public_blob(p) for p in identity_paths) if b}
            if cfg.identities_only:
                agent_keys = [k for k in agent_keys if k.asbytes() in identity_blobs]
            agent_by_blob = {k.asbytes(): k for k in agent_keys}

            candidates: list[tuple[str, Callable[[], paramiko.PKey | None]]] = []
            # Explicit identities first: use the agent's copy when it holds the
            # same key (no passphrase prompt), otherwise load the file.
            for path in identity_paths:
                blob = _public_blob(path)
                if blob and blob in agent_by_blob:
                    candidates.append((f"agent:{path.name}", lambda m=agent_by_blob[blob]: m))
                    continue
                candidates.append(
                    (
                        f"file:{path.name}",
                        lambda p=path: load_private_key(p, self.prompter, self.credentials, self.options.allow_keyring),
                    )
                )
            used = {name for name, _ in candidates}
            for key in agent_keys:
                name = f"agent:{key.get_name()}:{key.get_fingerprint().hex()[:12]}"
                if key.asbytes() in identity_blobs or name in used:
                    continue
                candidates.append((name, lambda k=key: k))

            remaining: list[str] | None = None
            for name, loader in candidates:
                key = loader()
                if key is None:
                    continue
                try:
                    result = transport.auth_publickey(cfg.user, key)
                except paramiko.BadAuthenticationType as exc:
                    return list(exc.allowed_types)
                except paramiko.AuthenticationException:
                    log.debug("Key %s rejected", name)
                    continue
                except paramiko.SSHException as exc:
                    log.info("Key %s failed: %s", name, exc)
                    continue
                if transport.is_authenticated():
                    return []
                remaining = list(result or [])
                break
            return remaining
        finally:
            if agent is not None:
                agent.close()

    def _stored_password(self, cfg: ResolvedConfig) -> str | None:
        if self._password_cache:
            return self._password_cache
        if self.options.allow_keyring and self.credentials.available:
            return self.credentials.get_password(cfg.user, cfg.hostname, cfg.port)
        return None

    def _ask_password(self, cfg: ResolvedConfig) -> SecretAnswer:
        answer = self.prompter.ask_password(
            f"{cfg.user}@{cfg.hostname}'s password:", self.options.allow_keyring and self.credentials.available
        )
        if answer is None:
            raise ConnectionCancelled("Password prompt cancelled")
        return answer

    def _password(self, transport: paramiko.Transport, cfg: ResolvedConfig) -> list[str] | None:
        stored = self._stored_password(cfg)
        attempts = 0
        while attempts < MAX_PASSWORD_ATTEMPTS:
            remember = False
            if stored is not None:
                password, stored = stored, None
                from_store = True
            else:
                answer = self._ask_password(cfg)
                password, remember = answer.value, answer.remember
                from_store = False
                attempts += 1
            try:
                result = transport.auth_password(cfg.user, password)
            except paramiko.BadAuthenticationType as exc:
                return list(exc.allowed_types)
            except paramiko.AuthenticationException:
                if from_store:
                    self._password_cache = None
                    if self.options.allow_keyring and self.credentials.available:
                        self.credentials.delete_password(cfg.user, cfg.hostname, cfg.port)
                continue
            self._password_cache = password
            if remember and self.options.allow_keyring and self.credentials.available:
                self.credentials.set_password(cfg.user, cfg.hostname, cfg.port, password)
            return [] if transport.is_authenticated() else list(result or [])
        return None

    def _keyboard_interactive(self, transport: paramiko.Transport, cfg: ResolvedConfig) -> list[str] | None:
        state = {"used_password": False, "remember": False, "password": None}

        def handler(title: str, instructions: str, prompts: list[tuple[str, bool]]) -> list[str]:
            if not prompts:
                return []
            # A single hidden "Password:" prompt -> behave like password auth.
            if len(prompts) == 1 and not prompts[0][1] and "password" in prompts[0][0].lower():
                if not state["used_password"]:
                    stored = self._stored_password(cfg)
                    if stored:
                        state["used_password"] = True
                        state["password"] = stored
                        return [stored]
                answer = self._ask_password(cfg)
                state["password"], state["remember"] = answer.value, answer.remember
                return [answer.value]
            answers = self.prompter.keyboard_interactive(title, instructions, list(prompts))
            if answers is None:
                raise ConnectionCancelled("Keyboard-interactive prompt cancelled")
            return answers

        for _attempt in range(MAX_PASSWORD_ATTEMPTS):
            try:
                result = transport.auth_interactive(cfg.user, handler)
            except paramiko.BadAuthenticationType as exc:
                return list(exc.allowed_types)
            except paramiko.AuthenticationException:
                if state["used_password"] and self.credentials.available and self.options.allow_keyring:
                    self.credentials.delete_password(cfg.user, cfg.hostname, cfg.port)
                    self._password_cache = None
                continue
            if state["password"]:
                self._password_cache = state["password"]
                if state["remember"] and self.options.allow_keyring and self.credentials.available:
                    self.credentials.set_password(cfg.user, cfg.hostname, cfg.port, state["password"])
            return [] if transport.is_authenticated() else list(result or [])
        return None
