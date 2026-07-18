"""User-facing (non Config / Admin) team management endpoints: /v1/me/*"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.auth.roles import at_least, rank, role_label
from core.db.engine import get_db
from core.db.models import LocalUser, Team, UserShadow
from core.db.repository import TeamRepository
from core.infra.responses import success_response
# Seam: team serialization is EE-only — on the CE single-tenant tree this module
# is missing, so team endpoints degrade to 404
try:
    from core.services.team_service import serialize_team_membership
except ModuleNotFoundError:
    serialize_team_membership = None

router = APIRouter(prefix="/v1/me", tags=["My Profile"])


class InviteBody(BaseModel):
    user_id: Optional[str] = None
    username: Optional[str] = None
    role: str = Field("member", pattern="^(member|admin)$")


def _require_team_member(db: Session, team_id: str, user_id: str) -> str:
    role = TeamRepository(db).get_member_role(team_id, user_id)
    if role is None:
        raise HTTPException(status_code=403, detail="你不在该团队中")
    return role


def _require_team_admin(db: Session, team_id: str, user_id: str) -> str:
    role = _require_team_member(db, team_id, user_id)
    if not at_least(role, "admin"):
        raise HTTPException(status_code=403, detail="需要团队管理员权限")
    return role


def _team_brief_with_count(team: Team, role: str) -> dict:
    if serialize_team_membership is None:
        raise HTTPException(status_code=404, detail="团队功能在当前版本不可用")
    member_count = len(team.members) if team.members is not None else 0
    return serialize_team_membership(team, role, member_count=member_count)


# ── Team list (brief) ─────────────────────────────────────────────────

@router.get("/teams", summary="我的团队列表")
async def my_teams(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出当前用户加入的所有团队（含角色与成员数的简要信息）。"""
    team_repo = TeamRepository(db)
    rows = team_repo.list_for_user(user.user_id)
    teams = [_team_brief_with_count(t, r) for t, r in rows]
    return success_response(data={"items": teams, "total": len(teams)})


# ── Single team detail ──────────────────────────────────────────────────────

@router.get("/teams/{team_id}", summary="团队详情")
async def team_detail(
    team_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取指定团队的详情（含当前用户角色与成员数）。需为该团队成员。"""
    my_role = _require_team_member(db, team_id, user.user_id)
    team = db.query(Team).filter(Team.team_id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="团队不存在")
    return success_response(data=_team_brief_with_count(team, my_role))


@router.get("/teams/{team_id}/members", summary="团队成员列表")
async def team_members(
    team_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出指定团队的成员（含角色、加入时间，并标记是否为本人）。需为该团队成员。"""
    my_role = _require_team_member(db, team_id, user.user_id)
    team_repo = TeamRepository(db)
    rows = team_repo.list_members(team_id)
    items = []
    for member, shadow in rows:
        items.append(
            {
                "user_id": shadow.user_id,
                "username": shadow.username,
                "avatar_url": shadow.avatar_url,
                "role": member.role,
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "is_self": shadow.user_id == user.user_id,
            }
        )
    return success_response(data={"items": items, "my_role": my_role})


# ── Invite member (admin / owner) ───────────────────────────────────────

@router.post("/teams/{team_id}/members", summary="邀请成员加入团队")
async def invite_member(
    team_id: str,
    body: InviteBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按 user_id 或 username 邀请用户加入团队。需团队管理员；仅所有者可直接授予 admin 角色。"""
    my_role = _require_team_admin(db, team_id, user.user_id)

    target: Optional[UserShadow] = None
    if body.user_id:
        target = db.query(UserShadow).filter(UserShadow.user_id == body.user_id).first()
    elif body.username:
        target = db.query(UserShadow).filter(UserShadow.username == body.username.strip()).first()
    if target is None:
        raise HTTPException(status_code=404, detail="未找到该用户，请先注册")

    requested_role = body.role
    if requested_role == "admin" and my_role != "owner":
        raise HTTPException(status_code=403, detail="仅团队所有者可将成员设为管理员")

    team_repo = TeamRepository(db)
    existing = team_repo.get_member_role(team_id, target.user_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"该用户已是团队{role_label(existing)}")

    team_repo.add_member(team_id, target.user_id, requested_role)

    return success_response(
        data={
            "team_id": team_id,
            "user_id": target.user_id,
            "username": target.username,
            "role": requested_role,
        }
    )


# ── Kick member / voluntary leave ─────────────────────────────────────────────────

@router.delete("/teams/{team_id}/members/{member_user_id}", summary="移除成员/退出团队")
async def remove_member(
    team_id: str,
    member_user_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """移除团队成员或本人退出团队。移除他人需管理员且角色高于对方；唯一所有者退出前须先转让所有权。"""
    my_role = _require_team_member(db, team_id, user.user_id)
    team_repo = TeamRepository(db)
    target_role = team_repo.get_member_role(team_id, member_user_id)
    if target_role is None:
        raise HTTPException(status_code=404, detail="该成员不在团队中")

    is_self = member_user_id == user.user_id

    if is_self:
        if my_role == "owner":
            from core.db.models import TeamMember

            owner_count = (
                db.query(TeamMember)
                .filter(TeamMember.team_id == team_id, TeamMember.role == "owner")
                .count()
            )
            if owner_count <= 1:
                raise HTTPException(status_code=409, detail="你是唯一所有者，请先转让所有权再退出")
    else:
        if not at_least(my_role, "admin"):
            raise HTTPException(status_code=403, detail="需要团队管理员权限")
        if rank(my_role) <= rank(target_role):
            raise HTTPException(status_code=403, detail=f"无法移除{role_label(target_role)}")

    team_repo.remove_member(team_id, member_user_id)
    return success_response(
        data={"team_id": team_id, "user_id": member_user_id, "self_leave": is_self}
    )


# ── User search (for finding invite targets) ──────────────────────────────────

@router.get("/users/search", summary="搜索用户（用于邀请）")
async def search_users(
    q: str = Query(..., min_length=2, max_length=64, description="用户名或真实姓名，≥2 字"),
    limit: int = Query(10, ge=1, le=20),
    user: UserContext = Depends(get_current_user),  # noqa: ARG001 — login required only
    db: Session = Depends(get_db),
):
    """按用户名或真实姓名模糊搜索用户，供邀请成员时查找目标。仅要求已登录。"""
    pattern = f"%{q.strip()}%"
    query = (
        db.query(UserShadow, LocalUser)
        .outerjoin(LocalUser, LocalUser.user_id == UserShadow.user_id)
        .filter(
            (UserShadow.username.ilike(pattern))
            | (LocalUser.real_name.ilike(pattern))
        )
        .order_by(UserShadow.username)
        .limit(limit)
    )
    items = []
    for shadow, local in query.all():
        items.append(
            {
                "user_id": shadow.user_id,
                "username": shadow.username,
                "real_name": local.real_name if local else None,
                "avatar_url": shadow.avatar_url,
            }
        )
    return success_response(data={"items": items})


