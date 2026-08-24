"""Memory management API

GET    /v1/memories                 list L2 procedural memories (vector retrieval layer)
GET    /v1/memories/profile         view the L1 Profile user profile (bounded markdown)
GET    /v1/memories/audit           view audit records (read/write/delete traces)
GET    /v1/memories/graph           view the L3 Graph (Neo4j entity relations)
GET    /v1/memories/settings        get the user's memory settings
PATCH  /v1/memories/settings        update the user's memory settings (switches)
PATCH  /v1/memories/profile/field   edit one L1 profile field
DELETE /v1/memories/profile/field   delete one L1 profile field
DELETE /v1/memories/graph/{id}      delete one L3 graph relation
DELETE /v1/memories                 clear all L2 memories
PATCH  /v1/memories/{id}            edit a single L2 memory
DELETE /v1/memories/{id}            delete a single L2 memory

The single-entry edit/delete routes exist because the turn card shows the user
every memory it just wrote and offers to fix or remove it. A memory system the
user cannot correct in the moment they see the mistake is one they stop
trusting, and the only alternative — wiping the layer — is not a correction.
"""

from typing import Optional

from core.auth.backend import UserContext, get_current_user
from core.config.settings import settings as _jx_settings
from core.db.engine import get_db
from core.infra.responses import error_response, success_response
from core.memory.context import MemoryContext
from core.memory.graph import delete_graph_relation, list_graph_relations
from core.memory.profile import delete_field as profile_delete_field
from core.memory.profile import get as profile_get
from core.memory.service import delete_all_memories, delete_memory, get_all_memories
from core.services import UserService
from core.services.memory_settings_service import MemorySettingsService
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/memories", tags=["memories"])


class MemorySettingsRequest(BaseModel):
    memory_enabled: bool | None = None
    memory_write_enabled: bool | None = None
    reranker_enabled: bool | None = None


class MemoryEditRequest(BaseModel):
    text: str = Field(..., min_length=1, description="记忆正文")
    # When present, the turn card that reported this memory is updated too, so
    # the transcript never shows a version of the memory that no longer exists.
    message_id: Optional[str] = Field(None, description="上报该记忆的消息 id")
    operation_id: Optional[str] = Field(None, description="客户端生成的本次修改幂等键")


class ProfileFieldRequest(BaseModel):
    key: str = Field(..., min_length=1, description="档案字段键，如 identity.dept")
    text: str = Field(..., min_length=1, description="字段取值")
    workspace_id: str = "default"
    message_id: Optional[str] = None
    operation_id: Optional[str] = None


# ── Register fixed paths first so they aren't mis-matched by /{memory_id} ──


def _is_reranker_available() -> bool:
    """Check if reranker endpoint is configured at the infra level."""
    try:
        from core.services.model_config import ModelConfigService

        cfg = ModelConfigService.get_instance().resolve("reranker")
        if cfg and cfg.base_url and cfg.model_name:
            return True
    except Exception:
        pass
    import os

    return bool(os.getenv("RERANKER_URL") and os.getenv("RERANKER_MODEL"))


@router.get("/settings", summary="获取记忆设置")
async def get_memory_settings(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取用户记忆 / 重排开关设置。"""
    svc = UserService(db)
    settings = svc.get_user_settings(str(user.user_id))
    availability = MemorySettingsService(db).availability()
    return success_response(
        data={
            "memory_enabled": settings.get("memory_enabled", False),
            "memory_write_enabled": settings.get("memory_write_enabled", False),
            **availability,
            "reranker_enabled": settings.get("reranker_enabled", False),
            "reranker_available": _is_reranker_available(),
        }
    )


@router.patch("/settings", summary="更新记忆设置")
async def update_memory_settings(
    body: MemorySettingsRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新用户记忆 / 重排开关设置（持久化到 users_shadow.metadata）。"""
    svc = UserService(db)
    patch: dict = {}
    if body.memory_enabled is not None:
        patch["memory_enabled"] = body.memory_enabled
    if body.memory_write_enabled is not None:
        patch["memory_write_enabled"] = body.memory_write_enabled
    if body.reranker_enabled is not None:
        patch["reranker_enabled"] = body.reranker_enabled
    if patch:
        MemorySettingsService(db).validate_patch(patch)
        svc.update_user_metadata(user_id=str(user.user_id), patch=patch)
    return success_response(
        data={
            **({"memory_enabled": body.memory_enabled} if body.memory_enabled is not None else {}),
            **(
                {"memory_write_enabled": body.memory_write_enabled}
                if body.memory_write_enabled is not None
                else {}
            ),
            **(
                {"reranker_enabled": body.reranker_enabled}
                if body.reranker_enabled is not None
                else {}
            ),
        }
    )


# ── List / clear / delete single ────────────────────────────────


@router.get("", summary="查询长期记忆列表")
async def list_memories(
    project_id: Optional[str] = Query(
        None, description="若指定，只返回属于该项目 workspace 的记忆"
    ),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """L2 长期记忆列表（mem0/Milvus 向量）——只存"做法/口径"，不存事实。

    返回的 item 会把 mem0 原始 metadata 拍平到顶层（layer / source / tags /
    confidentiality / ttl_days / evidence / why / applies_to / memory_type）方便
    前端分层展示；未知字段保持原样透传。

    ``project_id`` 给定时按 ``metadata.workspace_id == f"project:{project_id}"``
    过滤；不给则按 ``workspace_id="default"`` 过滤（避免把项目记忆混进默认空间）。

    ``enabled`` 的语义：mem0 全局可用 ∧ 当前 workspace 的读开关。项目模式下读
    项目自己的 ``memory_enabled``（缺省 True）；非项目模式下读用户的 setting。
    """
    if not _jx_settings.memory.enabled:
        return success_response(data={"enabled": False, "items": [], "count": 0})

    scope_user_id = str(user.user_id)
    if project_id:
        from core.services.project_scope import project_memory_policy

        policy = project_memory_policy(db, project_id, scope_user_id)
        if policy is None:
            return success_response(data={"enabled": False, "items": [], "count": 0})
        ws_enabled, scope_user_id = policy
    else:
        settings = UserService(db).get_user_settings(str(user.user_id))
        ws_enabled = bool(settings.get("memory_enabled", False))

    if not ws_enabled:
        return success_response(data={"enabled": False, "items": [], "count": 0})

    # Have mem0 / Milvus filter by workspace_id already at recall time, avoiding the bug
    # where cross-project memories crowd out the top_k cut and post-hoc client-side
    # filtering makes project memories "invisible".
    # Memories in the `default` workspace have no explicit metadata.workspace_id; that
    # legacy data is filtered client-side as a fallback (the mem0 metadata filter cannot
    # express "field missing" semantics).
    expected_ws = f"project:{project_id}" if project_id else "default"
    if project_id:
        raw_items = await get_all_memories(scope_user_id, workspace_id=expected_ws)
        filtered = raw_items  # already filtered on the Milvus side
    else:
        raw_items = await get_all_memories(scope_user_id)
        filtered = [
            it
            for it in raw_items
            if ((it.get("metadata") or {}).get("workspace_id") or "default") == "default"
        ]
    items = [_flatten_fact_metadata(it) for it in filtered]
    return success_response(data={"enabled": True, "items": items, "count": len(items)})


def _flatten_fact_metadata(item: dict) -> dict:
    """Flatten a mem0 item's metadata fields to the top level; unknown fields pass through as-is.

    ``author_user_id`` denotes the memory's real author and may be missing in legacy data.
    """
    if not isinstance(item, dict):
        return item
    meta = item.get("metadata") or {}
    return {
        **item,
        "layer": meta.get("layer", "L2"),
        "source": meta.get("source"),
        "tags": meta.get("tags") or [],
        "confidentiality": meta.get("confidentiality"),
        "ttl_days": meta.get("ttl_days"),
        "evidence": meta.get("evidence"),
        # A procedure's reason and the task family it holds for. Surfaced because
        # a rule without its reason can only be obeyed everywhere or nowhere.
        "memory_type": meta.get("memory_type") or "",
        "why": meta.get("why") or "",
        "applies_to": meta.get("applies_to") or "",
        "strength": meta.get("strength") or "",
        # Reinforcement bookkeeping: how many turns restated this rule, and when
        # it stops being recalled (mem0 native expiry; absent on legacy rows).
        "seen_count": meta.get("seen_count"),
        "expiration_date": meta.get("expiration_date"),
        "author_user_id": meta.get("author_user_id") or meta.get("user_id"),
    }


@router.delete("", summary="清空全部记忆")
async def remove_all_memories(user: UserContext = Depends(get_current_user)):
    """清空当前用户所有记忆。"""
    ok = await delete_all_memories(str(user.user_id))
    if not ok:
        return error_response(code=50002, message="清空失败", status_code=500)
    return success_response(data={"message": "已清空所有记忆"})


# ── L1 profile: one field at a time ───────────────────────────────────────


@router.patch("/profile/field", summary="修改单条档案记忆")
async def edit_profile_field(
    body: ProfileFieldRequest,
    user: UserContext = Depends(get_current_user),
):
    """改写 L1 档案里的一个字段（如 `identity.dept`）。"""
    ctx = MemoryContext(
        user_id=str(user.user_id),
        workspace_id=body.workspace_id,
        write_enabled=True,
        actor=str(user.user_id),
        message_id=body.message_id,
    )
    from core.memory.outbox import (
        consume_outbox_job,
        enqueue_profile_edit_job,
        kick_outbox_drain,
    )

    job_id = enqueue_profile_edit_job(ctx, body.key, body.text, operation_id=body.operation_id)
    outcome = await consume_outbox_job(job_id)
    if outcome["status"] == "quarantined":
        return error_response(code=50003, message="修改失败", status_code=500)
    if outcome["status"] != "succeeded":
        kick_outbox_drain()
        return success_response(
            data={"key": body.key, "text": body.text, "status": "queued", "job_id": job_id}
        )
    return success_response(
        data={"key": body.key, "text": body.text, "status": "succeeded", "job_id": job_id}
    )


@router.delete("/profile/field", summary="删除单条档案记忆")
async def remove_profile_field(
    key: str = Query(..., description="档案字段键"),
    workspace_id: str = Query("default"),
    message_id: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """删除 L1 档案里的一个字段。"""
    ctx = MemoryContext(
        user_id=str(user.user_id),
        workspace_id=workspace_id,
        write_enabled=True,
        actor=str(user.user_id),
    )
    ok = await profile_delete_field(ctx, key)
    if not ok:
        return error_response(code=50001, message="删除失败", status_code=500)
    if message_id:
        from core.evolution.settlement_store import drop_entry

        drop_entry(message_id, key)
    return success_response(data={"deleted": key})


# ── L3 graph: one relation at a time ──────────────────────────────────────


@router.delete("/graph/{relation_id}", summary="删除单条图谱关系")
async def remove_graph_relation(
    relation_id: str,
    message_id: Optional[str] = Query(None, description="上报该关系的消息 id"),
    user: UserContext = Depends(get_current_user),
):
    """删除当前用户默认作用域中的一条 L3 关系。"""
    ok = await delete_graph_relation(relation_id, scope_user_id=str(user.user_id))
    if not ok:
        return error_response(code=50001, message="删除失败", status_code=500)
    if message_id:
        from core.evolution.settlement_store import drop_entry

        drop_entry(message_id, relation_id)
    return success_response(data={"deleted": relation_id})


# ── L2 procedures: one entry at a time ────────────────────────────────────


@router.patch("/{memory_id}", summary="修改单条记忆")
async def edit_memory(
    memory_id: str,
    body: MemoryEditRequest,
    user: UserContext = Depends(get_current_user),
):
    """改写单条 L2 记忆的正文，保留其 id 与元数据。"""
    ctx = MemoryContext(
        user_id=str(user.user_id),
        write_enabled=True,
        actor=str(user.user_id),
        message_id=body.message_id,
    )
    from core.memory.outbox import (
        consume_outbox_job,
        enqueue_memory_edit_job,
        kick_outbox_drain,
    )

    job_id = enqueue_memory_edit_job(ctx, memory_id, body.text, operation_id=body.operation_id)
    outcome = await consume_outbox_job(job_id)
    if outcome["status"] == "quarantined":
        return error_response(code=50003, message="修改失败", status_code=500)
    if outcome["status"] != "succeeded":
        kick_outbox_drain()
        return success_response(
            data={"id": memory_id, "text": body.text, "status": "queued", "job_id": job_id}
        )
    return success_response(
        data={"id": memory_id, "text": body.text, "status": "succeeded", "job_id": job_id}
    )


@router.delete("/{memory_id}", summary="删除单条记忆")
async def remove_memory(
    memory_id: str,
    message_id: Optional[str] = Query(None, description="上报该记忆的消息 id"),
    user: UserContext = Depends(get_current_user),
):
    """删除单条记忆。"""
    ok = await delete_memory(memory_id)
    if not ok:
        return error_response(code=50001, message="删除失败", status_code=500)
    if message_id:
        from core.evolution.settlement_store import drop_entry

        drop_entry(message_id, memory_id)
    return success_response(data={"deleted": memory_id})


# ── Layered memory details ────────────────────────────────────────────────


@router.get("/profile", summary="查询用户档案记忆")
async def get_profile_memory(
    user: UserContext = Depends(get_current_user),
    workspace_id: str = Query("default", description="工作空间 id"),
):
    """L1 档案记忆：会话启动时冻结注入的用户画像 markdown。

    返回：{ enabled, workspace_id, content_md, length, max_chars }
    """
    if not _jx_settings.memory.enabled:
        return success_response(
            data={
                "enabled": False,
                "workspace_id": workspace_id,
                "content_md": "",
                "length": 0,
                "max_chars": _jx_settings.memory.profile_max_chars,
            }
        )
    content = await profile_get(str(user.user_id), workspace_id)
    return success_response(
        data={
            "enabled": True,
            "workspace_id": workspace_id,
            "content_md": content or "",
            "length": len(content or ""),
            "max_chars": _jx_settings.memory.profile_max_chars,
        }
    )


@router.get("/audit", summary="查询记忆审计记录")
async def list_memory_audit(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500, description="返回行数上限"),
    action: str | None = Query(
        None, description="按 action 过滤：read/write/update/delete/write_rejected/forget"
    ),
    layer: str | None = Query(None, description="按 layer 过滤：L1/L2/L3/session"),
):
    """审计记录：谁在什么时间对自己的记忆做了什么操作。

    原文不落 audit 表，只留 SHA256 content_hash；reason 字段记录抽取器 / 拒写原因等。
    """
    if not _jx_settings.memory.audit_enabled:
        return success_response(data={"enabled": False, "items": [], "count": 0})

    from core.db.models import MemoryAudit

    q = db.query(MemoryAudit).filter(MemoryAudit.user_id == str(user.user_id))
    if action:
        q = q.filter(MemoryAudit.action == action)
    if layer:
        q = q.filter(MemoryAudit.layer == layer)
    rows = q.order_by(MemoryAudit.ts.desc()).limit(limit).all()

    items = [
        {
            "id": r.id,
            "ts": r.ts.isoformat() if r.ts else None,
            "actor": r.actor,
            "action": r.action,
            "layer": r.layer,
            "memory_id": r.memory_id,
            "workspace_id": r.workspace_id,
            "chat_id": r.chat_id,
            "confidentiality": r.confidentiality,
            "content_hash": r.content_hash,
            "reason": r.reason,
        }
        for r in rows
    ]
    return success_response(data={"enabled": True, "items": items, "count": len(items)})


@router.get("/graph", summary="查询图谱记忆")
async def get_memory_graph(
    limit: int = Query(30, ge=1, le=200, description="返回关系条数"),
    project_id: Optional[str] = Query(None, description="若指定，返回该项目作用域的关系"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """L3 图谱记忆：当前用户或项目作用域的实体关系（Neo4j）。"""
    if not (_jx_settings.memory.enabled and _jx_settings.memory.graph_enabled):
        return success_response(data={"enabled": False, "relations": [], "count": 0})

    scope_user_id = str(user.user_id)
    workspace_id = "default"
    if project_id:
        from core.services.project_scope import project_memory_policy

        policy = project_memory_policy(db, project_id, scope_user_id)
        if policy is None:
            return success_response(data={"enabled": False, "relations": [], "count": 0})
        ws_enabled, scope_user_id = policy
        if not ws_enabled:
            return success_response(data={"enabled": False, "relations": [], "count": 0})
        workspace_id = f"project:{project_id}"
    else:
        user_settings = UserService(db).get_user_settings(str(user.user_id))
        if not bool(user_settings.get("memory_enabled", False)):
            return success_response(data={"enabled": False, "relations": [], "count": 0})

    relations = await list_graph_relations(
        scope_user_id,
        workspace_id=workspace_id,
        limit=limit,
    )
    return success_response(data={"enabled": True, "relations": relations, "count": len(relations)})
