"""Teams and memberships."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sshdesk_server.deps import current_user, get_db, get_team, membership, require_role
from sshdesk_server.models import Membership, Team, TeamHost, User
from sshdesk_server.schemas import MemberOut, RoleIn, TeamIn, TeamOut

router = APIRouter(prefix="/teams", tags=["teams"])


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "team"
    return slug


def team_out(db: Session, team: Team, role: str) -> TeamOut:
    members = db.scalar(select(func.count()).select_from(Membership).where(Membership.team_id == team.id)) or 0
    hosts = db.scalar(select(func.count()).select_from(TeamHost).where(TeamHost.team_id == team.id)) or 0
    return TeamOut(id=team.id, name=team.name, slug=team.slug, role=role, revision=team.revision, members=members, hosts=hosts)


@router.post("", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
def create_team(body: TeamIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> TeamOut:
    base = slugify(body.name)
    slug = base
    while db.scalar(select(Team).where(Team.slug == slug)):
        slug = f"{base}-{secrets.token_hex(2)}"
    team = Team(name=body.name.strip(), slug=slug)
    db.add(team)
    db.flush()
    db.add(Membership(team_id=team.id, user_id=user.id, role="owner"))
    db.commit()
    return team_out(db, team, "owner")


@router.get("", response_model=list[TeamOut])
def list_teams(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[TeamOut]:
    rows = db.execute(select(Team, Membership.role).join(Membership).where(Membership.user_id == user.id).order_by(Team.name)).all()
    return [team_out(db, team, role) for team, role in rows]


@router.patch("/{team_id}", response_model=TeamOut)
def rename_team(team_id: int, body: TeamIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> TeamOut:
    m = membership(db, team_id, user)
    require_role(m, "owner", "admin")
    team = get_team(db, team_id)
    team.name = body.name.strip()
    db.commit()
    return team_out(db, team, m.role)


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(team_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    require_role(membership(db, team_id, user), "owner")
    db.delete(get_team(db, team_id))
    db.commit()


@router.get("/{team_id}/members", response_model=list[MemberOut])
def list_members(team_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[MemberOut]:
    membership(db, team_id, user)
    rows = db.execute(select(Membership, User).join(User).where(Membership.team_id == team_id).order_by(User.email)).all()
    return [MemberOut(user_id=u.id, email=u.email, name=u.name, role=m.role) for m, u in rows]


def _owners(db: Session, team_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Membership).where(Membership.team_id == team_id, Membership.role == "owner")) or 0


@router.put("/{team_id}/members/{user_id}", response_model=MemberOut)
def set_role(team_id: int, user_id: int, body: RoleIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MemberOut:
    require_role(membership(db, team_id, user), "owner")
    target = db.scalar(select(Membership).where(Membership.team_id == team_id, Membership.user_id == user_id))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    if target.role == "owner" and body.role != "owner" and _owners(db, team_id) == 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A team needs at least one owner")
    target.role = body.role
    db.commit()
    return MemberOut(user_id=target.user.id, email=target.user.email, name=target.user.name, role=target.role)


@router.delete("/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(team_id: int, user_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    """Admins remove members; anyone can remove themselves (leave the team)."""
    mine = membership(db, team_id, user)
    if user_id != user.id:
        require_role(mine, "owner", "admin")
    target = db.scalar(select(Membership).where(Membership.team_id == team_id, Membership.user_id == user_id))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    if target.role == "owner" and mine.role != "owner" and user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only owners can remove owners")
    if target.role == "owner" and _owners(db, team_id) == 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The last owner cannot leave; transfer ownership or delete the team")
    db.delete(target)
    db.commit()
