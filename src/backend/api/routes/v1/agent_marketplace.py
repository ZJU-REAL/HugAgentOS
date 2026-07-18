"""Sub-agent marketplace (user side) — browse preset/community sub-agents and install them as private sub-agents.

GET    /v1/agent-marketplace/agents             List marketplace sub-agents (preset + community, including "already installed")
GET    /v1/agent-marketplace/categories         Category list
GET    /v1/agent-marketplace/agents/{slug}      A single marketplace sub-agent's detail (prompt + capability-binding preview)
POST   /v1/agent-marketplace/install            Install as a private sub-agent (clone + dependencies installed along with it)
POST   /v1/agent-marketplace/submissions        Submit a self-built sub-agent for marketplace listing (pending admin review)
GET    /v1/agent-marketplace/submissions        My listing-submission list
DELETE /v1/agent-marketplace/submissions/{id}   Withdraw a submission (pending/rejected can be withdrawn)

Permissions: browsing/viewing details is open to all logged-in users; **installing + submitting for listing require ``can_add_agent``**
(consistent with sub-agent creation; opened per user/team via Config admin "User Management / Team Management → Permission Config").
After installation the clone appears in the "Sub-agents" panel, with bound skills/tools already installed in place.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import get_current_user, UserContext
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError
from core.infra.responses import created_response, success_response
from core.services import agent_market_service as am
from core.services import marketplace_listing as ml

router = APIRouter(prefix="/v1/agent-marketplace", tags=["Agent Marketplace"])
logger = logging.getLogger(__name__)


def _require_can_add_agent(user_id: str, db: Session) -> None:
    # Personal explicit (User Management) → team default (Team Management) → off by default
    if not resolve_user_capabilities(db, user_id)["can_add_agent"]:
        raise AccessDeniedError(message="管理员未开放自建/安装子智能体功能", reason="can_add_agent_disabled")


@router.get("/agents", summary="子智能体市场列表")
async def list_agents(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = am.list_marketplace_agents(db, viewer_user_id=str(user.user_id))
    items = am.annotate_installed(items, db, owner_user_id=str(user.user_id))
    return success_response(data={"items": items, "categories": am.categories_from_items(items)})


@router.get("/categories", summary="子智能体市场分类")
async def list_categories(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(data=am.list_categories(db, viewer_user_id=str(user.user_id)))


@router.get("/agents/{slug}", summary="子智能体市场详情")
async def get_agent(
    slug: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ml.ensure_item_visible(db, ml.KIND_AGENT, slug, str(user.user_id), resource="marketplace_agent")
    data = am.get_agent_detail(db, slug)
    data["installed"] = am.is_installed(db, slug, owner_user_id=str(user.user_id))
    return success_response(data=data)


class InstallRequest(BaseModel):
    slug: str = Field(..., min_length=1, description="市场子智能体 slug")


@router.post("/install", status_code=201, summary="安装市场子智能体（私有克隆）")
async def install_agent(
    body: InstallRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把市场子智能体克隆为「仅自己可见可用」的私有子智能体（绑定的技能/工具随装）。需 ``can_add_agent`` 权限。"""
    _require_can_add_agent(str(user.user_id), db)
    ml.ensure_item_visible(db, ml.KIND_AGENT, body.slug, str(user.user_id), resource="marketplace_agent")
    result = am.install_marketplace_agent(
        db, body.slug, owner_user_id=str(user.user_id), operator_name=user.username,
    )
    return created_response(data=result)


# ── Submit for listing (community sharing) ──────────────────────────────────────

class SubmitRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, description="自己的子智能体 id")
    note: str = Field("", max_length=2000, description="给管理员的申请备注")
    category: str = Field(..., max_length=64, description="上架分类（必选，限固定 9 大分类）")
    summary: str = Field("", max_length=2000, description="市场展示摘要（留空取智能体简介）")


@router.post("/submissions", status_code=201, summary="申请把子智能体上架市场")
async def submit_agent(
    body: SubmitRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_can_add_agent(str(user.user_id), db)
    result = am.submit_to_marketplace(
        db,
        body.agent_id,
        owner_user_id=str(user.user_id),
        submitter_name=user.username or "",
        note=body.note,
        category=body.category,
        summary=body.summary,
    )
    return created_response(data=result)


@router.get("/submissions", summary="我的上架申请列表")
async def list_my_submissions(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(data={"items": am.list_my_submissions(db, str(user.user_id))})


@router.delete("/submissions/{submission_id}", summary="撤回上架申请")
async def withdraw_submission(
    submission_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    am.withdraw_submission(db, submission_id, owner_user_id=str(user.user_id))
    return success_response(data={"id": submission_id, "message": "申请已撤回"})
