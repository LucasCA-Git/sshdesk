"""Invites: admins invite an email; the invitee accepts with the code (sent by email or shared manually)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from sshdesk_server.config import Settings
from sshdesk_server.deps import current_user, get_db, get_settings, get_team, membership, require_role
from sshdesk_server.mailer import send_invite
from sshdesk_server.models import Invite, Membership, User
from sshdesk_server.routes.teams import team_out
from sshdesk_server.schemas import AcceptIn, InviteIn, InviteOut, TeamOut
from sshdesk_server.security import code_hash, new_invite_code

router = APIRouter(tags=["invites"])


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def invite_out(inv: Invite, code: str | None = None, email_sent: bool = False) -> InviteOut:
    return InviteOut(
        id=inv.id,
        team_id=inv.team_id,
        team_name=inv.team.name,
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at,
        invited_by=inv.invited_by.email if inv.invited_by else "",
        code=code,
        email_sent=email_sent,
    )


@router.post("/teams/{team_id}/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
def create_invite(
    team_id: int, body: InviteIn, user: User = Depends(current_user), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> InviteOut:
    require_role(membership(db, team_id, user), "owner", "admin")
    team = get_team(db, team_id)
    email = body.email.lower()
    existing_user = db.scalar(select(User).where(User.email == email))
    if existing_user and db.scalar(select(Membership).where(Membership.team_id == team_id, Membership.user_id == existing_user.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "This person is already a member")
    # A new invite for the same email replaces pending ones.
    for old in db.scalars(select(Invite).where(Invite.team_id == team_id, Invite.email == email, Invite.accepted_at.is_(None))):
        db.delete(old)
    code = new_invite_code()
    inv = Invite(
        team_id=team_id,
        email=email,
        role=body.role,
        code_hash=code_hash(code),
        invited_by_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(days=settings.invite_ttl_days),
    )
    db.add(inv)
    db.commit()
    sent = send_invite(settings, email, team.name, user.name or user.email, code)
    return invite_out(inv, code=code, email_sent=sent)


@router.get("/teams/{team_id}/invites", response_model=list[InviteOut])
def team_invites(team_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[InviteOut]:
    require_role(membership(db, team_id, user), "owner", "admin")
    rows = db.scalars(select(Invite).where(Invite.team_id == team_id, Invite.accepted_at.is_(None)).order_by(Invite.created_at.desc()))
    return [invite_out(i) for i in rows]


@router.delete("/teams/{team_id}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite(team_id: int, invite_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    require_role(membership(db, team_id, user), "owner", "admin")
    inv = db.get(Invite, invite_id)
    if inv is None or inv.team_id != team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    db.delete(inv)
    db.commit()


@router.get("/invites", response_model=list[InviteOut])
def my_invites(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[InviteOut]:
    """Pending invites addressed to the signed-in user's email (the code is still required to accept)."""
    now = datetime.now(UTC)
    rows = db.scalars(select(Invite).where(Invite.email == user.email, Invite.accepted_at.is_(None)))
    return [invite_out(i) for i in rows if _aware(i.expires_at) > now]


@router.post("/invites/accept", response_model=TeamOut)
def accept_invite(body: AcceptIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> TeamOut:
    if not request.app.state.invite_limiter.allow(str(user.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts, try again later")
    inv = db.scalar(select(Invite).where(Invite.code_hash == code_hash(body.code)))
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired invite code")
    if inv is None or inv.accepted_at is not None or _aware(inv.expires_at) < datetime.now(UTC):
        raise invalid
    if inv.email != user.email:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This invite was sent to another email address. Sign in with {inv.email}.")
    existing = db.scalar(select(Membership).where(Membership.team_id == inv.team_id, Membership.user_id == user.id))
    if existing is None:
        db.add(Membership(team_id=inv.team_id, user_id=user.id, role=inv.role))
    inv.accepted_at = datetime.now(UTC)
    db.commit()
    team = get_team(db, inv.team_id)
    role = existing.role if existing else inv.role
    return team_out(db, team, role)
