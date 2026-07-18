"""Skill marketplace (user side) -- browse preset/community skills and install them as private skills.

GET    /v1/marketplace/skills              list marketplace skills (preset + community, with "installed or not")
GET    /v1/marketplace/categories          category list
GET    /v1/marketplace/skills/{slug}       single marketplace skill detail (with SKILL.md preview, required credentials)
POST   /v1/marketplace/install             install as a private skill (owner=current user)
POST   /v1/marketplace/submissions         submit your own private skill to the marketplace (pending admin review)
GET    /v1/marketplace/submissions         my submission list
DELETE /v1/marketplace/submissions/{id}    withdraw a submission (pending/rejected can be withdrawn)

Permissions: install / submit requires the ``can_add_skill`` switch (same as self-service skills in the capability center).
Once installed, the skill automatically appears in the "Mine" section of ``/v1/catalog`` and is registered with the
agent -- no extra catalog operation needed.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import get_current_user, UserContext
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.infra.responses import created_response, success_response
from core.services import marketplace_service as mk
from core.services import marketplace_listing as ml
from core.infra.exceptions import AccessDeniedError

router = APIRouter(prefix="/v1/marketplace", tags=["Skill Marketplace"])
logger = logging.getLogger(__name__)


def _require_can_add_skill(user_id: str, db: Session) -> None:
    # personal explicit (user management) -> team default (team management) -> off by default
    if not resolve_user_capabilities(db, user_id)["can_add_skill"]:
        raise AccessDeniedError(message="管理员未开放自助添加技能功能", reason="can_add_skill_disabled")


@router.get("/skills", summary="技能市场列表")
async def list_skills(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出全部市场技能（预置+社区上架，按可见范围过滤），并标注当前用户是否已安装。"""
    items = mk.list_marketplace_skills(db, viewer_user_id=str(user.user_id))
    items = mk.annotate_installed(items, db, owner_user_id=str(user.user_id))
    return success_response(
        data={"items": items, "categories": mk.list_categories(db, viewer_user_id=str(user.user_id))}
    )


@router.get("/categories", summary="技能市场分类")
async def list_categories(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(data=mk.list_categories(db, viewer_user_id=str(user.user_id)))


@router.get("/skills/{slug}", summary="技能市场详情")
async def get_skill(
    slug: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ml.ensure_item_visible(db, ml.KIND_SKILL, slug, str(user.user_id), resource="marketplace_skill")
    data = mk.get_marketplace_skill(slug, db)
    data["installed"] = mk.is_installed(db, slug, owner_user_id=str(user.user_id))
    return success_response(data=data)


class InstallRequest(BaseModel):
    slug: str = Field(..., min_length=1, description="市场技能 slug")
    secrets: Dict[str, str] = Field(default_factory=dict, description="凭据键值（按 required_secrets 提供）")


@router.post("/install", status_code=201, summary="安装市场技能（私有）")
async def install_skill(
    body: InstallRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把市场技能安装为「仅自己可见可用」的私有技能。需 ``can_add_skill`` 权限。

    市场技能由管理员策划（预置 / 管理员上传上架），其依赖由**管理员侧统一管理**——安装后
    该私有技能已 ``is_enabled=True``，会被依赖聚合器纳入沙盒清单，管理员重建沙盒即装齐。
    因此**不再对用户做逐人依赖门控**（不创建「等待管理员安装依赖」请求、不软禁用），用户
    安装即直接可用。只有用户**自助上传**的外部技能才可能触发依赖检查。
    """
    _require_can_add_skill(str(user.user_id), db)
    ml.ensure_item_visible(db, ml.KIND_SKILL, body.slug, str(user.user_id), resource="marketplace_skill")
    result = mk.install_marketplace_skill(
        db, body.slug, owner_user_id=str(user.user_id), secrets=body.secrets
    )
    result.pop("dependencies", None)
    result["dep_pending"] = False
    return created_response(data=result)


# ── Submit for listing (community sharing) ──────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    skill_id: str = Field(..., min_length=1, description="自己的私有技能 id")
    note: str = Field("", max_length=2000, description="给管理员的申请备注")
    category: str = Field(..., max_length=64, description="上架分类（必选，限固定 8 大分类）")
    summary: str = Field("", max_length=2000, description="市场展示摘要（留空取技能描述）")


@router.post("/submissions", status_code=201, summary="申请把私有技能上架市场")
async def submit_skill(
    body: SubmitRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把自己的私有技能提交上架申请（内容快照），管理员审核通过后全员可安装。"""
    _require_can_add_skill(str(user.user_id), db)
    result = mk.submit_to_marketplace(
        db,
        body.skill_id,
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
    return success_response(data={"items": mk.list_my_submissions(db, str(user.user_id))})


@router.delete("/submissions/{submission_id}", summary="撤回上架申请")
async def withdraw_submission(
    submission_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    mk.withdraw_submission(db, submission_id, owner_user_id=str(user.user_id))
    return success_response(data={"id": submission_id, "message": "申请已撤回"})
