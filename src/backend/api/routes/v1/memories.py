"""Memory management API

GET    /v1/memories           list L2 Fact memories (vector retrieval layer)
GET    /v1/memories/profile   view the L1 Profile user profile (bounded markdown)
GET    /v1/memories/audit     view audit records (read/write/delete traces)
GET    /v1/memories/graph     view the L3 Graph (Neo4j entity relations)
GET    /v1/memories/settings  get the user's memory settings
PATCH  /v1/memories/settings  update the user's memory settings (switches)
DELETE /v1/memories           clear all L2 memories
DELETE /v1/memories/{id}      delete a single L2 memory
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from core.auth.backend import get_current_user, UserContext
from core.db.engine import get_db
from core.config.settings import settings as _jx_settings
from core.memory.profile import get as profile_get
from core.memory.service import (
    get_all_memories,
    delete_memory,
    delete_all_memories,
)
from core.infra.responses import success_response, error_response
from core.services import UserService
from core.services.memory_settings_service import MemorySettingsService

router = APIRouter(prefix="/v1/memories", tags=["memories"])


class MemorySettingsRequest(BaseModel):
    memory_enabled: bool | None = None
    memory_write_enabled: bool | None = None
    reranker_enabled: bool | None = None


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
    return success_response(data={
        "memory_enabled": settings.get("memory_enabled", False),
        "memory_write_enabled": settings.get("memory_write_enabled", False),
        **availability,
        "reranker_enabled": settings.get("reranker_enabled", False),
        "reranker_available": _is_reranker_available(),
    })


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
    return success_response(data={
        **({"memory_enabled": body.memory_enabled} if body.memory_enabled is not None else {}),
        **({"memory_write_enabled": body.memory_write_enabled} if body.memory_write_enabled is not None else {}),
        **({"reranker_enabled": body.reranker_enabled} if body.reranker_enabled is not None else {}),
    })


# ── List / clear / delete single ────────────────────────────────

@router.get("", summary="查询事实记忆列表")
async def list_memories(
    project_id: Optional[str] = Query(None, description="若指定，只返回属于该项目 workspace 的记忆"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """L2 Fact 事实记忆列表（mem0/Milvus 向量）。

    返回的 item 会把 mem0 原始 metadata 拍平到顶层（layer / source / tags /
    confidentiality / ttl_days / evidence）方便前端分层展示；未知字段保持原样透传。

    ``project_id`` 给定时按 ``metadata.workspace_id == f"project:{project_id}"``
    过滤；不给则按 ``workspace_id="default"`` 过滤（避免把项目记忆混进默认空间）。

    ``enabled`` 的语义：mem0 全局可用 ∧ 当前 workspace 的读开关。项目模式下读
    项目自己的 ``memory_enabled``（缺省 True）；非项目模式下读用户的 setting。
    """
    if not _jx_settings.memory.enabled:
        return success_response(data={"enabled": False, "items": [], "count": 0})

    # Resolve the workspace-level read switch + compute the mem0 scope_user_id
    # (for team projects scope = "team:<tid>" enables sharing; personal projects
    # and the default space use the real user_id)
    scope_user_id = str(user.user_id)
    if project_id:
        from core.db.models import Project as _Project
        _p = (
            db.query(_Project)
            .filter(_Project.project_id == project_id, _Project.deleted_at.is_(None))
            .first()
        )
        # Missing project → treat as disabled, avoiding returning data for an unknown project_id
        if _p is None:
            return success_response(data={"enabled": False, "items": [], "count": 0})
        ws_enabled = bool((_p.extra_data or {}).get("memory_enabled", True))
        if _p.kind == "team" and _p.team_id:
            scope_user_id = f"team:{_p.team_id}"
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
            it for it in raw_items
            if ((it.get("metadata") or {}).get("workspace_id") or "default") == "default"
        ]
    items = [_flatten_fact_metadata(it) for it in filtered]
    return success_response(data={"enabled": True, "items": items, "count": len(items)})


def _flatten_fact_metadata(item: dict) -> dict:
    """Flatten a mem0 item's metadata fields to the top level; unknown fields pass through as-is.

    In team projects ``author_user_id`` denotes the memory's real author; in personal
    projects and the default space it usually equals the top-level user_id. May be
    missing in legacy data.
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
        "author_user_id": meta.get("author_user_id") or meta.get("user_id"),
    }


@router.delete("", summary="清空全部记忆")
async def remove_all_memories(user: UserContext = Depends(get_current_user)):
    """清空当前用户所有记忆。"""
    ok = await delete_all_memories(str(user.user_id))
    if not ok:
        return error_response(code=50002, message="清空失败", status_code=500)
    return success_response(data={"message": "已清空所有记忆"})


@router.delete("/{memory_id}", summary="删除单条记忆")
async def remove_memory(memory_id: str, user: UserContext = Depends(get_current_user)):
    """删除单条记忆。"""
    ok = await delete_memory(memory_id)
    if not ok:
        return error_response(code=50001, message="删除失败", status_code=500)
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
        return success_response(data={
            "enabled": False,
            "workspace_id": workspace_id,
            "content_md": "",
            "length": 0,
            "max_chars": _jx_settings.memory.profile_max_chars,
        })
    content = await profile_get(str(user.user_id), workspace_id)
    return success_response(data={
        "enabled": True,
        "workspace_id": workspace_id,
        "content_md": content or "",
        "length": len(content or ""),
        "max_chars": _jx_settings.memory.profile_max_chars,
    })


@router.get("/audit", summary="查询记忆审计记录")
async def list_memory_audit(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500, description="返回行数上限"),
    action: str | None = Query(None, description="按 action 过滤：read/write/update/delete/write_rejected/forget"),
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

    items = [{
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
    } for r in rows]
    return success_response(data={"enabled": True, "items": items, "count": len(items)})


@router.get("/graph", summary="查询图谱记忆")
async def get_memory_graph(
    user: UserContext = Depends(get_current_user),
    limit: int = Query(30, ge=1, le=200, description="返回关系条数"),
):
    """L3 图谱记忆：当前用户的实体关系（Neo4j）。

    当前返回 `enabled` 状态 + 空 relations 列表。结构化 graph 查询需要 service 层
    暴露 `mem0.Memory.search(filters={"graph_only": True})`，下轮实现；前端 Tab 以
    `enabled` 字段决定显示占位或关系。
    """
    if not (_jx_settings.memory.enabled and _jx_settings.memory.graph_enabled):
        return success_response(data={"enabled": False, "relations": [], "count": 0})
    return success_response(data={"enabled": True, "relations": [], "count": 0})
