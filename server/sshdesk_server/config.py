"""Server settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _secret_key(data_dir: Path) -> str:
    """SECRET_KEY from env, or a random key persisted in DATA_DIR (so tokens survive restarts)."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = data_dir / "secret.key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    path.write_text(key, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    log.warning("SECRET_KEY not set; generated one in %s", path)
    return key


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "./data")))
    database_url: str = ""
    secret_key: str = ""
    token_ttl_hours: int = field(default_factory=lambda: int(os.environ.get("TOKEN_TTL_HOURS", "720")))
    invite_ttl_days: int = field(default_factory=lambda: int(os.environ.get("INVITE_TTL_DAYS", "7")))
    allow_registration: bool = field(default_factory=lambda: _bool("ALLOW_REGISTRATION", True))
    public_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_URL", "http://localhost:8080"))
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ.get("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("SMTP_FROM", "SSHDesk <no-reply@localhost>"))
    smtp_tls: str = field(default_factory=lambda: os.environ.get("SMTP_TLS", "starttls"))  # starttls | ssl | none

    def __post_init__(self) -> None:
        if not self.database_url:
            self.database_url = os.environ.get("DATABASE_URL") or f"sqlite:///{(self.data_dir / 'sshdesk.db').as_posix()}"
        if not self.secret_key:
            self.secret_key = _secret_key(self.data_dir)
        if len(self.secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters (e.g. `openssl rand -base64 48`)")

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host)
