"""Lab · Personal skill-distillation API.

POST   /v1/lab/skill-distill/jobs            Create a distillation job (chosen chats / all chats)
GET    /v1/lab/skill-distill/jobs            My job list
GET    /v1/lab/skill-distill/jobs/{job_id}   Job detail (with progress and artifact)
POST   /v1/lab/skill-distill/jobs/{job_id}/save    Persist the artifact as my private skill
POST   /v1/lab/skill-distill/jobs/{job_id}/cancel  Cancel
DELETE /v1/lab/skill-distill/jobs/{job_id}         Delete

A job is a PersonaDistillJob with kind='personal': target_user = requested_by = self.
"""

from __future__ import annotations

from typing import List, Optional, Union

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import PersonaDistillJob
from core.infra.exceptions import AccessDeniedError, BadRequestError, ResourceNotFoundError
from core.infra.responses import created_response, success_response
from core.services import persona_distillation_service as pds

router = APIRouter(prefix="/v1/lab/skill-distill", tags=["LabSkillDistill"])

_MAX_SELECTED_CHATS = 500


class CreateJobRequest(BaseModel):
    # Explicit list = chosen chats; "all" = all chats (server samples recent-first up to the cap)
    chat_ids: Union[List[str], str] = Field(..., description='会话 ID 列表，或字符串 "all"')
    hint: Optional[str] = Field(None, description="蒸馏侧重提示", max_length=500)
    include_project_memories: bool = Field(True, description="纳入所选会话关联项目中本人的记忆")


class SaveJobRequest(BaseModel):
    skill_content: Optional[str] = Field(None, description="编辑后的 SKILL.md 全文（不传用原产物）")
    enable: bool = Field(True, description="保存后立即启用")


def _ensure_lab_enabled(db: Session, user_id: str) -> None:
    """Server-side fallback check of lab permission: personal explicit → team default → default on."""
    from core.auth.capabilities import resolve_user_capabilities

    if not resolve_user_capabilities(db, str(user_id))["lab_enabled"]:
        raise AccessDeniedError("实验室功能未对当前账号开放")


def _job_to_dict(job: PersonaDistillJob, include_result: bool = False) -> dict:
    return pds.job_to_dict(job, include_result=include_result)


def _get_own_job(db: Session, job_id: str, user_id: str) -> PersonaDistillJob:
    job = pds.get_job(db, job_id)
    if job is None or job.kind != "personal" or job.target_user_id != str(user_id):
        raise ResourceNotFoundError("persona_distill_job", job_id)
    return job


@router.post("/jobs", status_code=status.HTTP_201_CREATED, summary="创建个人技能蒸馏作业")
async def create_job(
    body: CreateJobRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_lab_enabled(db, user.user_id)

    if isinstance(body.chat_ids, str):
        if body.chat_ids != "all":
            raise BadRequestError('chat_ids 必须是会话 ID 列表或 "all"')
        scope_chat_ids = None
    else:
        ids = [c.strip() for c in body.chat_ids if c and c.strip()]
        if not ids:
            raise BadRequestError("至少选择一个会话")
        if len(ids) > _MAX_SELECTED_CHATS:
            raise BadRequestError(f"最多选择 {_MAX_SELECTED_CHATS} 个会话")
        invalid = pds.validate_chat_ids(db, ids, str(user.user_id))
        if invalid:
            raise BadRequestError(f"以下会话不存在或不属于你：{', '.join(invalid[:5])}")
        scope_chat_ids = ids

    # Only one in-progress job allowed per user at a time
    active = (
        db.query(PersonaDistillJob)
        .filter(
            PersonaDistillJob.kind == "personal",
            PersonaDistillJob.target_user_id == str(user.user_id),
            PersonaDistillJob.status.in_(("queued", "running")),
        )
        .first()
    )
    if active:
        raise BadRequestError("已有进行中的蒸馏作业，请等待完成或先取消")

    scope = {
        "chat_ids": scope_chat_ids,
        "hint": (body.hint or "").strip(),
        "include_project_memories": body.include_project_memories,
    }
    job = pds.create_job(
        db,
        kind="personal",
        target_user_id=str(user.user_id),
        requested_by=str(user.user_id),
        scope=scope,
    )
    pds.start_job_background(job.job_id)
    return created_response(data=_job_to_dict(job))


@router.get("/jobs", summary="我的蒸馏作业列表")
async def list_jobs(
    limit: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    jobs = pds.list_jobs(
        db, kind="personal", target_user_id=str(user.user_id), limit=limit
    )
    return success_response(data={"items": [_job_to_dict(j) for j in jobs], "count": len(jobs)})


@router.get("/jobs/{job_id}", summary="作业详情（含产物）")
async def get_job(
    job_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_own_job(db, job_id, user.user_id)
    return success_response(data=_job_to_dict(job, include_result=True))


@router.post("/jobs/{job_id}/save", summary="保存产物为我的私有技能")
async def save_job(
    job_id: str,
    body: SaveJobRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_lab_enabled(db, user.user_id)
    job = _get_own_job(db, job_id, user.user_id)
    skill = pds.save_personal_skill(
        db, job, edited_content=body.skill_content, enable=body.enable
    )
    return success_response(
        data={
            "skill_id": skill.skill_id,
            "display_name": skill.display_name,
            "is_enabled": skill.is_enabled,
            "job": _job_to_dict(job),
        }
    )


@router.post("/jobs/{job_id}/cancel", summary="取消作业")
async def cancel_job(
    job_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_own_job(db, job_id, user.user_id)
    job = pds.cancel_job(db, job.job_id)
    return success_response(data=_job_to_dict(job))


@router.delete("/jobs/{job_id}", summary="删除作业")
async def delete_job(
    job_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_own_job(db, job_id, user.user_id)
    pds.delete_job(db, job.job_id)
    return success_response(data={"deleted": job_id})
