"""Shared hosts of a team + the sync endpoint used by the desktop app."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sshdesk_server.deps import current_user, get_db, get_team, membership, require_role
from sshdesk_server.models import Membership, Team, TeamHost, User
from sshdesk_server.schemas import HostIn, HostOut, SyncOut, SyncTeam, UserOut

router = APIRouter(tags=["hosts"])


def host_out(h: TeamHost, db: Session) -> HostOut:
    by = db.get(User, h.updated_by_id) if h.updated_by_id else None
    return HostOut(
        id=h.id,
        alias=h.alias,
        hostname=h.hostname,
        user=h.user,
        port=h.port,
        proxy_jump=h.proxy_jump,
        identity_file=h.identity_file,
        group=h.group,
        description=h.description,
        options=[tuple(o) for o in (h.options or [])],
        host_keys=list(h.host_keys or []),
        updated_at=h.updated_at,
        updated_by=by.email if by else "",
    )


def _apply(h: TeamHost, body: HostIn, user: User) -> None:
    h.alias = body.alias
    h.hostname = body.hostname
    h.user = body.user
    h.port = body.port
    h.proxy_jump = body.proxy_jump
    h.identity_file = body.identity_file
    h.group = body.group
    h.description = body.description
    h.options = [list(o) for o in body.options]
    h.host_keys = list(body.host_keys)
    h.updated_by_id = user.id


def _commit(db: Session, team: Team, alias: str) -> None:
    team.revision += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"A host named '{alias}' already exists in this team") from None


@router.get("/teams/{team_id}/hosts", response_model=list[HostOut])
def list_hosts(team_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[HostOut]:
    membership(db, team_id, user)
    return [host_out(h, db) for h in get_team(db, team_id).hosts]


@router.post("/teams/{team_id}/hosts", response_model=HostOut, status_code=status.HTTP_201_CREATED)
def create_host(team_id: int, body: HostIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> HostOut:
    require_role(membership(db, team_id, user), "owner", "admin")
    team = get_team(db, team_id)
    h = TeamHost(team_id=team_id)
    _apply(h, body, user)
    db.add(h)
    _commit(db, team, body.alias)
    return host_out(h, db)


@router.put("/teams/{team_id}/hosts/{host_id}", response_model=HostOut)
def update_host(team_id: int, host_id: int, body: HostIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> HostOut:
    require_role(membership(db, team_id, user), "owner", "admin")
    h = db.get(TeamHost, host_id)
    if h is None or h.team_id != team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Host not found")
    _apply(h, body, user)
    _commit(db, get_team(db, team_id), body.alias)
    return host_out(h, db)


@router.delete("/teams/{team_id}/hosts/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_host(team_id: int, host_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    require_role(membership(db, team_id, user), "owner", "admin")
    h = db.get(TeamHost, host_id)
    if h is None or h.team_id != team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Host not found")
    team = get_team(db, team_id)
    db.delete(h)
    team.revision += 1
    db.commit()


@router.get("/sync", response_model=SyncOut, responses={304: {"description": "Not modified"}})
def sync(
    response: Response,
    if_none_match: str | None = Header(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SyncOut | Response:
    """Everything the desktop app needs in one call. Unchanged data answers 304 (ETag)."""
    rows = db.execute(select(Team, Membership.role).join(Membership).where(Membership.user_id == user.id).order_by(Team.name)).all()
    etag = '"' + "-".join(f"{t.id}.{t.revision}.{t.name}.{role}" for t, role in rows) + f'-u{user.token_version}"'
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    teams = [SyncTeam(id=t.id, name=t.name, slug=t.slug, role=role, revision=t.revision, hosts=[host_out(h, db) for h in t.hosts]) for t, role in rows]
    response.headers["ETag"] = etag
    return SyncOut(user=UserOut.model_validate(user), teams=teams)
