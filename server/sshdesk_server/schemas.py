"""Request/response schemas and validation of shared host definitions.

Shared hosts end up in every member's SSH config, so the server only accepts
an allow-list of harmless options. Anything that could run a command on a
member's machine (ProxyCommand, LocalCommand, KnownHostsCommand, Match exec,
Include...) or weaken host key checking is rejected. The client validates
again before writing (defence in depth).
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._:%\[\]-]{1,255}$")
USER_RE = re.compile(r"^[A-Za-z0-9._@-]{0,120}$")
JUMP_RE = re.compile(r"^[A-Za-z0-9._@:,\[\]-]{0,255}$")
KEYWORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,63}$")
HOST_KEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}$"
)

ALLOWED_OPTIONS = {
    "addkeystoagent", "ciphers", "compression", "connecttimeout", "dynamicforward", "forwardagent",
    "forwardx11", "hostkeyalgorithms", "identitiesonly", "kexalgorithms", "localforward", "loglevel", "macs",
    "preferredauthentications", "pubkeyacceptedalgorithms", "remotecommand", "remoteforward", "requesttty",
    "sendenv", "serveralivecountmax", "serveraliveinterval", "setenv", "tcpkeepalive",
}


def _no_control_chars(value: str) -> str:
    if any(ord(c) < 32 or c in '"#' for c in value):
        raise ValueError("must not contain control characters, quotes or #")
    return value


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserOut


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    name: str = Field(default="", max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TeamOut(BaseModel):
    id: int
    name: str
    slug: str
    role: str
    revision: int
    members: int
    hosts: int


class MemberOut(BaseModel):
    user_id: int
    email: str
    name: str
    role: str


class RoleIn(BaseModel):
    role: str = Field(pattern="^(owner|admin|member)$")


class InviteIn(BaseModel):
    email: EmailStr
    role: str = Field(default="member", pattern="^(admin|member)$")


class InviteOut(BaseModel):
    id: int
    team_id: int
    team_name: str
    email: str
    role: str
    expires_at: datetime
    invited_by: str = ""
    code: str | None = None  # only returned once, to the admin who created it
    email_sent: bool = False


class AcceptIn(BaseModel):
    code: str = Field(min_length=8, max_length=32)


class HostIn(BaseModel):
    alias: str
    hostname: str
    user: str = ""
    port: int = Field(default=22, ge=1, le=65535)
    proxy_jump: str = ""
    identity_file: str = Field(default="", max_length=255)
    group: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2000)
    options: list[tuple[str, str]] = Field(default_factory=list, max_length=50)
    host_keys: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("alias")
    @classmethod
    def _alias(cls, v: str) -> str:
        if not ALIAS_RE.match(v):
            raise ValueError("alias: letters, digits, . _ - (no spaces or wildcards)")
        return v

    @field_validator("hostname")
    @classmethod
    def _hostname(cls, v: str) -> str:
        if not HOSTNAME_RE.match(v):
            raise ValueError("invalid hostname")
        return v

    @field_validator("user")
    @classmethod
    def _user(cls, v: str) -> str:
        if not USER_RE.match(v):
            raise ValueError("invalid user")
        return v

    @field_validator("proxy_jump")
    @classmethod
    def _jump(cls, v: str) -> str:
        if not JUMP_RE.match(v):
            raise ValueError("invalid ProxyJump")
        return v

    @field_validator("identity_file", "group", "description")
    @classmethod
    def _text(cls, v: str) -> str:
        return _no_control_chars(v) if v else v

    @field_validator("options")
    @classmethod
    def _options(cls, v: list[tuple[str, str]]) -> list[tuple[str, str]]:
        for key, value in v:
            if not KEYWORD_RE.match(key) or key.lower() not in ALLOWED_OPTIONS:
                raise ValueError(f"option '{key}' is not allowed in shared hosts")
            _no_control_chars(value)
            if len(value) > 500:
                raise ValueError(f"option '{key}' value too long")
        return v

    @field_validator("host_keys")
    @classmethod
    def _keys(cls, v: list[str]) -> list[str]:
        cleaned = []
        for key in v:
            parts = key.split()
            key = " ".join(parts[:2])
            if not HOST_KEY_RE.match(key):
                raise ValueError("host key must look like 'ssh-ed25519 AAAA...'")
            cleaned.append(key)
        return cleaned


class HostOut(HostIn):
    id: int
    updated_at: datetime
    updated_by: str = ""


class SyncTeam(BaseModel):
    id: int
    name: str
    slug: str
    role: str
    revision: int
    hosts: list[HostOut]


class SyncOut(BaseModel):
    user: UserOut
    teams: list[SyncTeam]


Token.model_rebuild()
