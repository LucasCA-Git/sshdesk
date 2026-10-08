"""Accounts: create account, sign in, profile, password change."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from sshdesk_server.config import Settings
from sshdesk_server.deps import current_user, get_db, get_settings
from sshdesk_server.models import User
from sshdesk_server.schemas import ChangePasswordIn, LoginIn, RegisterIn, Token, UserOut
from sshdesk_server.security import DUMMY_HASH, create_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _token(user: User, settings: Settings) -> Token:
    token, expires = create_token(user.id, user.token_version, settings.secret_key, settings.token_ttl_hours)
    return Token(access_token=token, expires_at=expires, user=UserOut.model_validate(user))


def _client_key(request: Request, email: str) -> str:
    host = request.client.host if request.client else "?"
    return f"{host}|{email.lower()}"


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Token:
    if not settings.allow_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is disabled on this server")
    if not request.app.state.register_limiter.allow(request.client.host if request.client else "?"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts, try again later")
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    return _token(user, settings)


@router.post("/login", response_model=Token)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Token:
    if not request.app.state.login_limiter.allow(_client_key(request, body.email)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts, try again in a minute")
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None:
        verify_password(DUMMY_HASH, body.password)  # same cost whether or not the user exists
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not verify_password(user.password_hash, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return _token(user, settings)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/password", response_model=Token)
def change_password(
    body: ChangePasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> Token:
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # signs out every other device
    db.commit()
    return _token(user, settings)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    user.token_version += 1
    db.commit()
