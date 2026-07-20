"""Ontology harness APIs: user opt-in plus Admin/CE asset governance."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Literal

from api.deps import require_admin, require_ontology_governance, user_can_manage_ontology_governance
from core.auth.backend import UserContext, get_current_user, require_auth
from core.config.settings import settings
from core.db.engine import get_db
from core.db.models import OntologyDraft, OntologyEnforcementEvent, OntologyReviewRun
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.responses import success_response
from core.services import UserService
from core.services.ontology_service import OntologyService
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

router = APIRouter(tags=["Ontologies"])


class OntologySettingsRequest(BaseModel):
    ontology_enabled: bool
    ontology_pack_ids: list[str] | None = Field(default=None, max_length=20)


class OntologyVersionRequest(BaseModel):
    document: dict[str, Any]
    activate: bool = False


class OntologyWorkingDraftRequest(BaseModel):
    document: dict[str, Any]
    draft_version_id: str | None = Field(default=None, max_length=64)
    expected_checksum: str | None = Field(default=None, min_length=64, max_length=64)


class OntologyPackFlagsRequest(BaseModel):
    is_enabled: bool | None = None
    is_default: bool | None = None


class OntologyDraftReviewRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=2000)


class OntologyEvolutionGenerateRequest(BaseModel):
    min_occurrences: int = Field(default=3, ge=2, le=20)
    limit: int = Field(default=500, ge=10, le=2000)
    model_name: str | None = Field(default=None, max_length=255)


class OntologyBuildValidationRequest(BaseModel):
    asset_type: str = Field(pattern="^(skill|tool|subagent)$")
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4000)
    instructions: str = Field(default="", max_length=20000)
    tool_names: list[str] = Field(default_factory=list, max_length=200)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=100)
    skill_ids: list[str] = Field(default_factory=list, max_length=100)
    plugin_ids: list[str] = Field(default_factory=list, max_length=100)
    ontology_tags: list[str] = Field(default_factory=list, max_length=100)
    output_schema: dict[str, Any] | None = None
    tool_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _pack_dict(row, versions: list | None = None) -> dict[str, Any]:
    data = {
        "pack_id": row.pack_id,
        "name": row.name,
        "domain": row.domain,
        "description": row.description,
        "is_enabled": row.is_enabled,
        "is_default": row.is_default,
        "active_version_id": row.active_version_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if versions is not None:
        data["versions"] = [_version_dict(item) for item in versions]
        working_draft = next((item for item in versions if item.status == "draft"), None)
        data["working_draft_version_id"] = (
            working_draft.version_id if working_draft is not None else None
        )
    return data


def _version_dict(row, *, include_content: bool = False) -> dict[str, Any]:
    data = {
        "version_id": row.version_id,
        "pack_id": row.pack_id,
        "version": row.version,
        "checksum": row.checksum,
        "status": row.status,
        "validation_report": row.validation_report or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "activated_at": row.activated_at.isoformat() if row.activated_at else None,
    }
    if include_content:
        data["content"] = row.content
    return data


@router.get("/v1/ontologies/settings", summary="获取当前用户的本体校验设置")
async def get_ontology_settings(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = UserService(db).get_user_settings(str(user.user_id))
    selected = settings.get("ontology_pack_ids") or []
    active = OntologyService(db).repo.get_active_versions(selected or None)
    return success_response(
        data={
            "ontology_enabled": bool(settings.get("ontology_enabled", False)),
            "ontology_pack_ids": selected,
            "available": bool(active),
            "active_packs": [
                {"pack_id": row.pack_id, "version_id": row.version_id, "version": row.version}
                for row in active
            ],
        }
    )


@router.patch("/v1/ontologies/settings", summary="更新当前用户的本体校验设置")
async def update_ontology_settings(
    body: OntologySettingsRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = OntologyService(db)
    pack_ids = body.ontology_pack_ids or []
    missing = [pack_id for pack_id in pack_ids if service.repo.get_pack(pack_id) is None]
    if missing:
        raise BadRequestError("包含不存在的 Domain Pack", data={"pack_ids": missing})
    active = service.repo.get_active_versions(pack_ids or None)
    if body.ontology_enabled and not active:
        raise BadRequestError("当前没有可启用的已激活 Domain Pack")
    patch: dict[str, Any] = {"ontology_enabled": body.ontology_enabled}
    if body.ontology_pack_ids is not None:
        patch["ontology_pack_ids"] = pack_ids
    UserService(db).update_user_metadata(str(user.user_id), patch)
    return success_response(
        data={
            **patch,
            "available": bool(active),
            "active_pack_count": len(active),
        }
    )


@router.get("/v1/ontologies/runtime/preview", summary="预览当前用户本轮将注入的本体策略")
async def preview_ontology_runtime(
    task: str = Query(..., min_length=1, max_length=4000),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = UserService(db).get_user_settings(str(user.user_id))
    if not settings.get("ontology_enabled", False):
        return success_response(data={"enabled": False, "packs": [], "review_level": "none"})
    from core.services.ontology_service import build_user_ontology_runtime

    _, runtime = build_user_ontology_runtime(
        user_id=str(user.user_id),
        task=task,
        db=db,
    )
    return success_response(data=runtime)


@router.get("/v1/ontologies/tags", summary="获取可选的受控本体标签")
async def list_ontology_tag_options(
    asset_kind: Literal["tool", "skill", "subagent"] = Query(...),
    _: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = OntologyService(db).list_asset_tag_options(asset_kind)
    return success_response(data={"asset_kind": asset_kind, "items": items})


@router.get("/v1/ontologies/governance/access", summary="CE 本体治理访问探针")
async def ontology_governance_access(
    user: UserContext | None = Depends(require_auth(False)),
    db: Session = Depends(get_db),
):
    """Let the settings page hide global governance controls from unauthorized users."""
    allowed = bool(user) and user_can_manage_ontology_governance(db, user.user_id)
    return success_response(data={"allowed": allowed, "edition": settings.edition.edition})


@router.get(
    "/v1/ontologies/governance",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页列出 Domain Pack 与版本",
)
@router.get(
    "/v1/admin/ontologies",
    dependencies=[Depends(require_admin)],
    summary="列出 Domain Pack 与版本",
)
async def list_ontology_packs(db: Session = Depends(get_db)):
    service = OntologyService(db)
    return success_response(
        data={
            "items": [
                _pack_dict(row, service.repo.list_versions(row.pack_id))
                for row in service.repo.list_packs()
            ]
        }
    )


@router.get(
    "/v1/ontologies/governance/tags",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页获取受控本体标签",
)
@router.get(
    "/v1/admin/ontologies/tags",
    dependencies=[Depends(require_admin)],
    summary="获取管理端可选的受控本体标签",
)
async def list_admin_ontology_tag_options(
    asset_kind: Literal["tool", "skill", "subagent"] = Query(...),
    db: Session = Depends(get_db),
):
    items = OntologyService(db).list_asset_tag_options(asset_kind)
    return success_response(data={"asset_kind": asset_kind, "items": items})


@router.get(
    "/v1/ontologies/governance/metrics",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页查询本体闭环治理指标",
)
@router.get(
    "/v1/admin/ontologies/metrics",
    dependencies=[Depends(require_admin)],
    summary="查询本体闭环治理指标",
)
async def get_ontology_metrics(db: Session = Depends(get_db)):
    event_decisions = Counter(
        {
            decision: count
            for decision, count in db.query(
                OntologyEnforcementEvent.decision,
                func.count(OntologyEnforcementEvent.event_id),
            )
            .group_by(OntologyEnforcementEvent.decision)
            .all()
        }
    )
    review_verdicts = Counter(
        {
            verdict: count
            for verdict, count in db.query(
                OntologyReviewRun.verdict,
                func.count(OntologyReviewRun.review_id),
            )
            .group_by(OntologyReviewRun.verdict)
            .all()
        }
    )
    drafts = db.query(OntologyDraft).all()
    draft_statuses = Counter(row.review_status for row in drafts)
    source_stats: dict[str, Counter] = defaultdict(Counter)
    for draft in drafts:
        source_stats[draft.source_type][draft.review_status] += 1
    source_acceptance = {}
    for source, counts in source_stats.items():
        decided = counts["approved"] + counts["rejected"]
        source_acceptance[source] = {
            "pending": counts["pending"],
            "approved": counts["approved"],
            "rejected": counts["rejected"],
            "acceptance_rate": round(counts["approved"] / decided, 4) if decided else None,
        }

    cutoff = datetime.utcnow() - timedelta(days=30)
    daily: dict[str, Counter] = defaultdict(Counter)
    recent_events = (
        db.query(OntologyEnforcementEvent.decision, OntologyEnforcementEvent.created_at)
        .filter(OntologyEnforcementEvent.created_at >= cutoff)
        .all()
    )
    recent_reviews = (
        db.query(OntologyReviewRun.verdict, OntologyReviewRun.created_at)
        .filter(OntologyReviewRun.created_at >= cutoff)
        .all()
    )
    for decision, created_at in recent_events:
        if created_at and created_at.replace(tzinfo=None) >= cutoff:
            daily[created_at.date().isoformat()][f"event_{decision}"] += 1
    for verdict, created_at in recent_reviews:
        if created_at and created_at.replace(tzinfo=None) >= cutoff:
            daily[created_at.date().isoformat()][f"review_{verdict}"] += 1

    return success_response(
        data={
            "events_total": sum(event_decisions.values()),
            "event_decisions": dict(event_decisions),
            "reviews_total": sum(review_verdicts.values()),
            "review_verdicts": dict(review_verdicts),
            "drafts_total": len(drafts),
            "draft_statuses": dict(draft_statuses),
            "materialized_total": sum(
                1 for row in drafts if (row.proposal or {}).get("materialized_version_id")
            ),
            "source_acceptance": source_acceptance,
            "daily_30d": [{"date": date, **dict(counts)} for date, counts in sorted(daily.items())],
        }
    )


@router.post(
    "/v1/ontologies/governance/build/validate",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页校验技能、工具或子智能体定义",
)
@router.post(
    "/v1/admin/ontologies/build/validate",
    dependencies=[Depends(require_admin)],
    summary="用激活的 Domain Pack 校验技能、工具或子智能体定义",
)
async def validate_ontology_build_asset(
    body: OntologyBuildValidationRequest,
    db: Session = Depends(get_db),
):
    from core.ontology.build_validator import OntologyBuildValidator

    report = OntologyBuildValidator(db).validate(
        asset_type=body.asset_type,  # type: ignore[arg-type]
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        tool_names=body.tool_names,
        mcp_server_ids=body.mcp_server_ids,
        skill_ids=body.skill_ids,
        plugin_ids=body.plugin_ids,
        ontology_tags=body.ontology_tags,
        output_schema=body.output_schema,
        tool_schemas=body.tool_schemas,
    )
    return success_response(data=report.as_dict())


@router.post(
    "/v1/ontologies/governance/validate",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页校验 Domain Pack",
)
@router.post(
    "/v1/admin/ontologies/validate",
    dependencies=[Depends(require_admin)],
    summary="校验 Domain Pack",
)
async def validate_ontology_pack(body: dict[str, Any], db: Session = Depends(get_db)):
    _, report = OntologyService(db).validate_document(body)
    return success_response(data=report)


@router.post(
    "/v1/ontologies/governance/versions",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页导入 Domain Pack 新版本",
)
@router.post(
    "/v1/admin/ontologies/versions",
    dependencies=[Depends(require_admin)],
    summary="导入 Domain Pack 新版本",
)
async def create_ontology_version(
    body: OntologyVersionRequest,
    db: Session = Depends(get_db),
):
    row = OntologyService(db).create_version(body.document, activate=body.activate)
    return success_response(data=_version_dict(row, include_content=True))


@router.put(
    "/v1/ontologies/governance/{pack_id}/draft",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页创建或更新 Domain Pack 工作草稿",
)
@router.put(
    "/v1/admin/ontologies/{pack_id}/draft",
    dependencies=[Depends(require_admin)],
    summary="创建或更新 Domain Pack 工作草稿",
)
async def save_ontology_working_draft(
    pack_id: str,
    body: OntologyWorkingDraftRequest,
    db: Session = Depends(get_db),
):
    row, created = OntologyService(db).save_working_draft(
        pack_id,
        body.document,
        draft_version_id=body.draft_version_id,
        expected_checksum=body.expected_checksum,
    )
    return success_response(data={**_version_dict(row, include_content=True), "created": created})


@router.delete(
    "/v1/ontologies/governance/{pack_id}/draft/{version_id}",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页放弃 Domain Pack 工作草稿",
)
@router.delete(
    "/v1/admin/ontologies/{pack_id}/draft/{version_id}",
    dependencies=[Depends(require_admin)],
    summary="放弃 Domain Pack 工作草稿",
)
async def discard_ontology_working_draft(
    pack_id: str,
    version_id: str,
    db: Session = Depends(get_db),
):
    OntologyService(db).discard_working_draft(pack_id, version_id)
    return success_response(data={"pack_id": pack_id, "version_id": version_id})


@router.post(
    "/v1/ontologies/governance/{pack_id}/versions/{version_id}/activate",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页激活 Domain Pack 版本",
)
@router.post(
    "/v1/admin/ontologies/{pack_id}/versions/{version_id}/activate",
    dependencies=[Depends(require_admin)],
    summary="激活 Domain Pack 版本",
)
async def activate_ontology_version(
    pack_id: str,
    version_id: str,
    db: Session = Depends(get_db),
):
    row = OntologyService(db).activate(pack_id, version_id)
    return success_response(data=_version_dict(row, include_content=True))


@router.patch(
    "/v1/ontologies/governance/{pack_id}",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页更新 Domain Pack 启用/默认状态",
)
@router.patch(
    "/v1/admin/ontologies/{pack_id}",
    dependencies=[Depends(require_admin)],
    summary="更新 Domain Pack 启用/默认状态",
)
async def update_ontology_pack(
    pack_id: str,
    body: OntologyPackFlagsRequest,
    db: Session = Depends(get_db),
):
    row = OntologyService(db).set_pack_flags(
        pack_id,
        is_enabled=body.is_enabled,
        is_default=body.is_default,
    )
    return success_response(data=_pack_dict(row))


@router.get(
    "/v1/ontologies/governance/{pack_id}/versions/{version_id}/export",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页导出 Domain Pack 版本",
)
@router.get(
    "/v1/admin/ontologies/{pack_id}/versions/{version_id}/export",
    dependencies=[Depends(require_admin)],
    summary="导出 Domain Pack 版本",
)
async def export_ontology_version(
    pack_id: str,
    version_id: str,
    db: Session = Depends(get_db),
):
    row = OntologyService(db).repo.get_version(version_id)
    if not row or row.pack_id != pack_id:
        raise ResourceNotFoundError("ontology_pack_version", version_id)
    return success_response(data=row.content)


@router.get(
    "/v1/ontologies/governance/events",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页查询本体门禁审计事件",
)
@router.get(
    "/v1/admin/ontologies/events",
    dependencies=[Depends(require_admin)],
    summary="查询本体门禁审计事件",
)
async def list_ontology_events(
    limit: int = Query(100, ge=1, le=500),
    chat_id: str | None = None,
    decision: str | None = None,
    db: Session = Depends(get_db),
):
    rows = OntologyService(db).repo.list_events(limit=limit, chat_id=chat_id, decision=decision)
    return success_response(
        data={
            "items": [
                {
                    "event_id": row.event_id,
                    "user_id": row.user_id,
                    "chat_id": row.chat_id,
                    "pack_id": row.pack_id,
                    "version_id": row.version_id,
                    "rule_id": row.rule_id,
                    "stage": row.stage,
                    "event_type": row.event_type,
                    "decision": row.decision,
                    "mode": row.mode,
                    "target": row.target,
                    "latency_ms": row.latency_ms,
                    "details": row.details or {},
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
        }
    )


@router.get(
    "/v1/ontologies/governance/reviews",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页查询本体评审记录",
)
@router.get(
    "/v1/admin/ontologies/reviews",
    dependencies=[Depends(require_admin)],
    summary="查询本体评审记录",
)
async def list_ontology_reviews(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    rows = OntologyService(db).repo.list_reviews(limit=limit)
    return success_response(
        data={
            "items": [
                {
                    "review_id": row.review_id,
                    "chat_id": row.chat_id,
                    "level": row.level,
                    "subject_type": row.subject_type,
                    "verdict": row.verdict,
                    "evidence": row.evidence or [],
                    "feedback": row.feedback,
                    "reviewers": row.reviewers or [],
                    "latency_ms": row.latency_ms,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
        }
    )


@router.get(
    "/v1/ontologies/governance/drafts",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页查询本体演进草案",
)
@router.get(
    "/v1/admin/ontologies/drafts",
    dependencies=[Depends(require_admin)],
    summary="查询待人工审查的本体演进草案",
)
async def list_ontology_drafts(
    status: str | None = Query(None, pattern="^(pending|approved|rejected)$"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    rows = OntologyService(db).repo.list_drafts(status=status, limit=limit)
    return success_response(
        data={
            "items": [
                {
                    "draft_id": row.draft_id,
                    "pack_id": row.pack_id,
                    "source_type": row.source_type,
                    "candidate_type": row.candidate_type,
                    "proposal": row.proposal,
                    "evidence": row.evidence,
                    "value_score": row.value_score,
                    "review_status": row.review_status,
                    "reviewer_comment": row.reviewer_comment,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
        }
    )


@router.post(
    "/v1/ontologies/governance/drafts/{draft_id}/review",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页审核本体演进草案",
)
@router.post(
    "/v1/admin/ontologies/drafts/{draft_id}/review",
    dependencies=[Depends(require_admin)],
    summary="人工通过或驳回本体演进草案",
)
async def review_ontology_draft(
    draft_id: str,
    body: OntologyDraftReviewRequest,
    db: Session = Depends(get_db),
):
    row = OntologyService(db).repo.get_draft(draft_id)
    if not row:
        raise ResourceNotFoundError("ontology_draft", draft_id)
    if row.review_status != "pending":
        raise BadRequestError("该演进草案已经完成审核，不能重复修改裁决")
    row.review_status = "approved" if body.approved else "rejected"
    row.reviewer_comment = body.comment
    row.reviewed_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.commit()
    return success_response(data={"draft_id": row.draft_id, "review_status": row.review_status})


@router.post(
    "/v1/ontologies/governance/evolution/generate",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页生成本体演进草案",
)
@router.post(
    "/v1/admin/ontologies/evolution/generate",
    dependencies=[Depends(require_admin)],
    summary="从重复门禁证据生成本体演进草案",
)
async def generate_ontology_evolution_drafts(
    body: OntologyEvolutionGenerateRequest,
    db: Session = Depends(get_db),
):
    from core.services.ontology_evolution_service import OntologyEvolutionService

    result = await OntologyEvolutionService(db).generate_candidates(
        min_occurrences=body.min_occurrences,
        limit=body.limit,
        model_name=body.model_name,
    )
    return success_response(data=result)


@router.post(
    "/v1/ontologies/governance/drafts/{draft_id}/materialize",
    dependencies=[Depends(require_ontology_governance)],
    summary="CE 设置页物化已批准的本体演进草案",
)
@router.post(
    "/v1/admin/ontologies/drafts/{draft_id}/materialize",
    dependencies=[Depends(require_admin)],
    summary="把已批准演进草案物化为未激活的新版本",
)
async def materialize_ontology_draft(
    draft_id: str,
    db: Session = Depends(get_db),
):
    from core.services.ontology_evolution_service import OntologyEvolutionService

    row = OntologyEvolutionService(db).materialize_approved_draft(draft_id)
    return success_response(data=_version_dict(row, include_content=True))
