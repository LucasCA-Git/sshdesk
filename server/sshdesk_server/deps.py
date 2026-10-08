"""FastAPI dependencies: settings, DB session, current user, team membership."""

from __future__ import annotations

from collections.abc import Iterator

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from sshdesk_server.config import Settings
from sshdesk_server.models import Membership, Team, User
from sshdesk_server.security import decode_token

bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.session()


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    try:
        payload = decode_token(creds.credentials, settings.secret_key)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized from None
    user = db.get(User, user_id)
    if user is None or payload.get("ver", 0) != user.token_version:
        raise unauthorized
    return user


def membership(db: Session, team_id: int, user: User) -> Membership:
    m = db.scalar(select(Membership).where(Membership.team_id == team_id, Membership.user_id == user.id))
    if m is None:
        # 404 instead of 403: do not reveal which teams exist
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
    return m


def require_role(m: Membership, *roles: str) -> None:
    if m.role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role: {' or '.join(roles)}")


def get_team(db: Session, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
    return team
