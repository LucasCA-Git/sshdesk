"""Password hashing (Argon2id), JWT access tokens and invite codes."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
ALGORITHM = "HS256"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# A real hash to compare against when the user does not exist (constant-ish timing).
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def create_token(user_id: int, token_version: int, secret: str, ttl_hours: int) -> tuple[str, datetime]:
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
    payload = {"sub": str(user_id), "ver": token_version, "exp": expires, "iat": datetime.now(UTC)}
    return jwt.encode(payload, secret, algorithm=ALGORITHM), expires


def decode_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})


def new_invite_code() -> str:
    """Human friendly code, e.g. ``K7QH-M2XP-9TWA`` (~60 bits)."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return "-".join(raw[i : i + 4] for i in range(0, 12, 4))


def normalize_code(code: str) -> str:
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    return "-".join(cleaned[i : i + 4] for i in range(0, len(cleaned), 4))


def code_hash(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


class RateLimiter:
    """Small in-memory sliding-window limiter (per key), enough for login/invite brute force."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()
