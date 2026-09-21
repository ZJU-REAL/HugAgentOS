"""Admin prompt management API routes.

Provides CRUD for system prompt parts managed via the admin backend.
DB records override filesystem prompt files; deleting a DB record
restores the filesystem version.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from api.deps import require_system_settings as require_config
from core.db.engine import get_db
from core.infra.responses import success_response
from core.services import prompt_version_service as pvs
from core.services.system_config import code_capability_enabled
from fastapi import APIRouter, Depends, HTTPException
from prompts.prompt_config import load_prompt_config
from prompts.provider import _FILE_CONTENT_CACHE, FilesystemPromptProvider
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/admin/prompts", tags=["Admin Prompts"])
logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _invalidate_prompt_cache():
    """Clear prompt caches so changes take effect immediately."""
    from prompts.prompt_runtime import invalidate_prompt_cache

    invalidate_prompt_cache()
    _FILE_CONTENT_CACHE.clear()


def _resolve_prompt_dir() -> Path:
    """Resolve the filesystem prompt directory from config."""
    config = load_prompt_config()
    raw = getattr(config.system_prompt, "prompt_dir", None) or "./prompts/prompt_text/default"
    from prompts.prompt_runtime import _resolve_prompt_dir

    return _resolve_prompt_dir(raw)


def _load_fs_parts() -> Dict[str, Dict[str, Any]]:
    """Load prompt parts from the filesystem based on config."""
    config = load_prompt_config()
    parts_list = config.system_prompt.parts or []
    prompt_dir = _resolve_prompt_dir()

    fs_parts: Dict[str, Dict[str, Any]] = {}
    for idx, part_id in enumerate(parts_list):
        part_id = part_id.strip()
        if not part_id:
            continue
        # Try to read from filesystem
        provider = FilesystemPromptProvider(prompt_dir=prompt_dir, strict_vars=False)
        content = provider.get_prompt(part_id, "system", vars={})
        # Generate display name from part_id
        # e.g. "system/00_role" -> "00_role"
        display = part_id.split("/")[-1] if "/" in part_id else part_id
        fs_parts[part_id] = {
            "part_id": part_id,
            "content": content,
            "display_name": display,
            "sort_order": idx * 10,
            "is_enabled": True,
            "source": "file",
        }
    return fs_parts


def _require_active_system_version(db: Session) -> Dict[str, Any]:
    """Return the active 'system' version, seeding defaults if the pool is empty."""
    pvs.seed_from_filesystem(db=db)
    active = pvs.get_active_version("system", db=db)
    if not active:
        raise HTTPException(status_code=500, detail="no active system prompt version configured")
    return active


def _persist_active_system(version: Dict[str, Any], db: Session) -> Dict[str, Any]:
    """Save updated parts/metadata back into the active system version."""
    saved = pvs.upsert_version(
        "system",
        version["id"],
        name=version.get("name"),
        description=version.get("description"),
        parts=version.get("parts") or [],
        db=db,
    )
    try:
        from prompts.prompt_runtime import invalidate_prompt_cache

        invalidate_prompt_cache()
    except Exception:
        pass
    return saved


# ── Request schemas ─────────────────────────────────────────────────────────


class PartUpsertRequest(BaseModel):
    content: str = Field(..., description="Markdown content")
    display_name: str = Field(..., description="Display name")
    sort_order: int = Field(0, description="Sort order")
    is_enabled: bool = Field(True, description="Enabled flag")


class OrderUpdateRequest(BaseModel):
    order: List[Dict[str, Any]] = Field(..., description="List of {part_id, sort_order}")


class PromptImportRequest(BaseModel):
    parts: List[Dict[str, Any]] = Field(..., description="Array of prompt part objects to import")
    overwrite: bool = Field(True, description="Overwrite existing parts")


# ── Routes ──────────────────────────────────────────────────────────────────


def _tool_name_from_schema(schema: Dict[str, Any]) -> str:
    func = schema.get("function") if isinstance(schema, dict) else None
    if isinstance(func, dict):
        return str(func.get("name") or "")
    return ""


def _render_tool_schema_appendix(tool_schemas: List[Dict[str, Any]]) -> str:
    """Render tool schemas as an audit appendix for the admin preview."""
    if not tool_schemas:
        return ""

    lines: List[str] = [
        "## 运行时工具描述（tool schemas）",
        "",
        "以下工具描述由模型的工具 schema 通道注入，不是 system prompt 文本；"
        "后台预览将其展开，便于审计完整运行时上下文。",
    ]
    for schema in tool_schemas:
        func = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(func, dict):
            continue
        name = str(func.get("name") or "").strip()
        if not name:
            continue
        desc = str(func.get("description") or "").strip()
        params = func.get("parameters")
        param_names: List[str] = []
        if isinstance(params, dict):
            props = params.get("properties")
            if isinstance(props, dict):
                param_names = [str(k) for k in props.keys()]
        lines.append("")
        lines.append(f"### {name}")
        if desc:
            lines.append(desc)
        if param_names:
            lines.append("参数：" + ", ".join(f"`{p}`" for p in param_names))
    return "\n".join(lines).strip()


async def _runtime_prompt_preview(db: Session, *, approval_mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Build a runtime preview by instantiating the main agent.

    If MCP/model setup is unavailable, callers fall back to static assembly.
    """
    clients: List[Any] = []
    try:
        from core.llm.agent_factory import create_agent_executor

        agent, clients = await create_agent_executor(
            current_user_id="admin_preview",
            chat_id="admin_prompt_preview",
            approval_mode=approval_mode,
        )
        toolkit = getattr(agent, "toolkit", None)
        tool_schemas = toolkit.get_json_schemas() if toolkit else []
        skill_prompt = ""
        if toolkit and hasattr(toolkit, "get_agent_skill_prompt"):
            skill_prompt = toolkit.get_agent_skill_prompt() or ""

        system_prompt = (
            getattr(agent, "_jx_compaction_system_prompt", None) or getattr(agent, "sys_prompt", None) or getattr(agent, "_sys_prompt", None) or ""
        )
        if not system_prompt:
            return None

        tool_appendix = _render_tool_schema_appendix(tool_schemas)
        audit_prompt = system_prompt
        if tool_appendix:
            audit_prompt += (
                "\n\n<!-- ↓↓↓ 工具描述由 tool schemas 注入；以下为后台审计展开 ↓↓↓ -->\n\n"
                + tool_appendix
            )

        active = pvs.get_active_version("system", db=db)
        code_active = pvs.get_active_version("code_exec", db=db)
        enabled_parts = len(
            [
                p
                for p in (active.get("parts") if active else []) or []
                if p.get("is_enabled", True) and (p.get("content") or "").strip()
            ]
        )
        code_seg = pvs.render_kind_segment("code_exec", db) if code_capability_enabled() else ""
        part_count = (
            enabled_parts
            + (1 if code_seg else 0)
            + (1 if skill_prompt else 0)
            + (1 if tool_appendix else 0)
        )
        return {
            "prompt": audit_prompt,
            "system_prompt": system_prompt,
            "part_count": part_count,
            "char_count": len(audit_prompt),
            "system_prompt_char_count": len(system_prompt),
            "skill_prompt_char_count": len(skill_prompt),
            "tool_schema_char_count": len(tool_appendix),
            "active_version": (f"{active['kind']}/{active['id']}" if active else None),
            "code_capability_active_version": (
                f"{code_active['kind']}/{code_active['id']}" if code_active else None
            ),
            "code_capability_appended": bool(code_seg),
            "skill_prompt_appended": bool(skill_prompt),
            "tool_schema_appended": bool(tool_appendix),
            "tool_schema_count": len(tool_schemas),
            "tool_names": [
                name for name in (_tool_name_from_schema(s) for s in tool_schemas) if name
            ],
            "tool_schemas": tool_schemas,
            "preview_mode": "runtime",
        }
    except Exception as exc:
        logger.warning("runtime prompt preview failed; using static fallback: %s", exc)
        return None
    finally:
        if clients:
            try:
                from core.llm.mcp_manager import close_clients

                await close_clients(clients)
            except Exception as exc:
                logger.warning("runtime prompt preview client cleanup failed: %s", exc)


@router.get("/parts", dependencies=[Depends(require_config)], summary="提示词分段列表")
def list_parts(db: Session = Depends(get_db)):
    """列出当前激活的 system 版本的所有提示词分段。

    数据真源为 ContentBlock(id=prompt_versions)，不再读取 AdminPromptPart，
    每个分段都归属于激活的版本池版本。
    """
    active = _require_active_system_version(db)
    parts = active.get("parts") or []
    items: List[Dict[str, Any]] = []
    for p in sorted(parts, key=lambda x: x.get("sort_order") or 0):
        items.append(
            {
                "part_id": p.get("part_id"),
                "content": p.get("content") or "",
                "display_name": p.get("display_name") or (p.get("part_id") or "").split("/")[-1],
                "sort_order": int(p.get("sort_order") or 0),
                "is_enabled": bool(p.get("is_enabled", True)),
                "source": "database",
                "updated_at": active.get("updated_at"),
                "created_by": None,
            }
        )
    return success_response(data=items)


@router.get("/active", dependencies=[Depends(require_config)], summary="激活版本概览")
def get_active_info(db: Session = Depends(get_db)):
    """返回各 kind 当前激活提示词版本的概要（id、名称、分段数、更新时间）。"""
    pvs.seed_from_filesystem(db=db)
    payload = pvs._load_payload(db)  # noqa: SLF001
    active = payload.get("active") or {}
    result = {}
    for kind, vid in active.items():
        v = pvs.get_version(kind, vid, db=db) if vid else None
        result[kind] = {
            "id": vid,
            "name": (v or {}).get("name"),
            "parts_count": len((v or {}).get("parts") or []),
            "updated_at": (v or {}).get("updated_at"),
        }
    return success_response(data=result)


@router.get("/export", dependencies=[Depends(require_config)], summary="导出提示词分段")
def export_prompts(db: Session = Depends(get_db)):
    """将当前激活的 system 版本的提示词分段导出为 JSON 数组。"""
    active = _require_active_system_version(db)
    items = []
    for p in sorted(active.get("parts") or [], key=lambda x: x.get("sort_order") or 0):
        items.append(
            {
                "part_id": p.get("part_id"),
                "content": p.get("content") or "",
                "display_name": p.get("display_name") or (p.get("part_id") or "").split("/")[-1],
                "sort_order": int(p.get("sort_order") or 0),
                "is_enabled": bool(p.get("is_enabled", True)),
            }
        )
    return success_response(data=items)


@router.post("/import", dependencies=[Depends(require_config)], summary="导入提示词分段")
def import_prompts(req: PromptImportRequest, db: Session = Depends(get_db)):
    """将提示词分段导入到激活的 system 版本。

    overwrite=True（默认）时覆盖同 part_id 的分段并追加新分段；
    overwrite=False 时跳过已存在的 part_id。
    """
    active = _require_active_system_version(db)
    parts = list(active.get("parts") or [])
    by_id = {(p.get("part_id") or "").strip(): idx for idx, p in enumerate(parts)}
    created = 0
    updated = 0
    for item in req.parts:
        pid = (item.get("part_id") or "").strip()
        if not pid:
            continue
        new_part = {
            "part_id": pid,
            "content": item.get("content", ""),
            "display_name": item.get("display_name", pid.split("/")[-1]),
            "sort_order": int(item.get("sort_order") or 0),
            "is_enabled": bool(item.get("is_enabled", True)),
        }
        if pid in by_id:
            if not req.overwrite:
                continue
            parts[by_id[pid]] = new_part
            updated += 1
        else:
            parts.append(new_part)
            by_id[pid] = len(parts) - 1
            created += 1
    active["parts"] = parts
    _persist_active_system(active, db)
    logger.info(
        "admin_prompts_imported (active=%s): created=%d updated=%d",
        active.get("id"),
        created,
        updated,
    )
    return success_response(
        data={"created": created, "updated": updated, "message": "Import complete"}
    )


@router.get(
    "/parts/{part_id:path}", dependencies=[Depends(require_config)], summary="提示词分段详情"
)
def get_part(part_id: str, db: Session = Depends(get_db)):
    """获取激活 system 版本中的单个提示词分段。

    同时返回文件系统上的参考内容（v4 on-disk，若有），便于 UI 展示未改动的"原始参考版"。
    """
    active = _require_active_system_version(db)
    found = next(
        (p for p in (active.get("parts") or []) if (p.get("part_id") or "").strip() == part_id),
        None,
    )
    fs_parts = _load_fs_parts()
    fs_data = fs_parts.get(part_id)
    if not found and not fs_data:
        raise HTTPException(status_code=404, detail=f"Part not found: {part_id}")

    current = found or fs_data
    return success_response(
        data={
            "current": {
                "part_id": current.get("part_id"),
                "content": current.get("content") or "",
                "display_name": current.get("display_name") or part_id.split("/")[-1],
                "sort_order": int(current.get("sort_order") or 0),
                "is_enabled": bool(current.get("is_enabled", True)),
                "source": "database" if found else "file",
            },
            "filesystem_content": fs_data["content"] if fs_data else None,
        }
    )


@router.put(
    "/parts/{part_id:path}", dependencies=[Depends(require_config)], summary="保存提示词分段"
)
def upsert_part(part_id: str, req: PartUpsertRequest, db: Session = Depends(get_db)):
    """在当前激活的 system 版本中新建或更新一个提示词分段。"""
    active = _require_active_system_version(db)
    parts = list(active.get("parts") or [])
    found_idx = next(
        (i for i, p in enumerate(parts) if (p.get("part_id") or "").strip() == part_id),
        None,
    )
    new_part = {
        "part_id": part_id,
        "content": req.content,
        "display_name": req.display_name,
        "sort_order": int(req.sort_order),
        "is_enabled": bool(req.is_enabled),
    }
    if found_idx is None:
        parts.append(new_part)
    else:
        parts[found_idx] = new_part
    active["parts"] = parts
    _persist_active_system(active, db)
    logger.info("admin_prompt_part_upserted (active=%s): %s", active.get("id"), part_id)
    return success_response(data={"part_id": part_id, "message": "Part saved"})


@router.delete(
    "/parts/{part_id:path}", dependencies=[Depends(require_config)], summary="删除提示词分段"
)
def delete_part(part_id: str, db: Session = Depends(get_db)):
    """从激活的 system 版本中移除一个提示词分段。"""
    active = _require_active_system_version(db)
    parts = list(active.get("parts") or [])
    new_parts = [p for p in parts if (p.get("part_id") or "").strip() != part_id]
    if len(new_parts) == len(parts):
        raise HTTPException(status_code=404, detail=f"Part not found in active version: {part_id}")
    active["parts"] = new_parts
    _persist_active_system(active, db)
    logger.info("admin_prompt_part_deleted (active=%s): %s", active.get("id"), part_id)
    return success_response(
        data={"part_id": part_id, "message": "Part removed from active version"}
    )


@router.put("/order", dependencies=[Depends(require_config)], summary="提示词分段排序")
def update_order(req: OrderUpdateRequest, db: Session = Depends(get_db)):
    """批量更新激活 system 版本中各提示词分段的 sort_order 排序。"""
    active = _require_active_system_version(db)
    parts = list(active.get("parts") or [])
    order_map: Dict[str, int] = {}
    for item in req.order:
        pid = item.get("part_id")
        order = item.get("sort_order")
        if pid and order is not None:
            order_map[str(pid)] = int(order)
    if not order_map:
        return success_response(data={"message": "No order changes"})
    for p in parts:
        pid = (p.get("part_id") or "").strip()
        if pid in order_map:
            p["sort_order"] = order_map[pid]
    active["parts"] = parts
    _persist_active_system(active, db)
    logger.info(
        "admin_prompt_order_updated (active=%s): %d items", active.get("id"), len(order_map)
    )
    return success_response(data={"message": "Order updated"})


@router.post("/preview", dependencies=[Depends(require_config)], summary="预览运行时提示词")
async def preview_prompt(db: Session = Depends(get_db)):
    """基于当前 DB + 文件系统状态预览运行时指令面。

    优先实例化主 agent，返回真实 ``sys_prompt``、AgentScope 自动追加的技能
    prompt，以及工具 schema 描述的审计展开；若运行时装配失败，再退回静态
    DB + 文件系统拼接。
    """
    runtime = await _runtime_prompt_preview(db)
    if runtime:
        return success_response(data=runtime)

    def _finalize(prompt: str, part_count: int, base: Dict[str, Any]):
        seg = ""
        if code_capability_enabled():
            try:
                seg = pvs.render_kind_segment("code_exec", db) or ""
            except Exception:
                seg = ""
        appended = bool(seg)
        if appended:
            prompt = (
                prompt
                + "\n\n<!-- ↓↓↓ 代码执行能力段（CODE_CAPABILITY_ENABLED 全模式"
                + "注入，运行时由 agent_factory 追加；编辑请到「代码执行」提示词版本池）"
                + " ↓↓↓ -->\n\n"
                + seg
            )
            part_count += 1
        base.update(
            {
                "prompt": prompt,
                "system_prompt": prompt,
                "part_count": part_count,
                "char_count": len(prompt),
                "code_capability_appended": appended,
                "skill_prompt_appended": False,
                "tool_schema_appended": False,
                "tool_schema_count": 0,
                "tool_names": [],
                "tool_schemas": [],
                "preview_mode": "static_fallback",
            }
        )
        return success_response(data=base)

    # Prefer the active version from the prompt_version pool
    active = pvs.get_active_version("system", db=db)
    if active and active.get("parts"):
        chunks: List[str] = []
        for p in active["parts"]:
            if not p.get("is_enabled", True):
                continue
            content = (p.get("content") or "").strip()
            if content:
                chunks.append(content)
        full_prompt = "\n\n".join(chunks)
        return _finalize(
            full_prompt,
            len(chunks),
            {
                "active_version": f"{active['kind']}/{active['id']}",
            },
        )

    parts = _load_fs_parts().values()
    chunks = [p["content"].strip() for p in parts if p.get("is_enabled", True) and p["content"].strip()]
    return _finalize("\n\n".join(chunks), len(chunks), {})


# ── Version pool routes (ContentBlock-backed, multi-kind) ───────────────────


class PromptPartPayload(BaseModel):
    part_id: str
    display_name: Optional[str] = None
    content: str = ""
    sort_order: int = 0
    is_enabled: bool = True


class VersionUpsertRequest(BaseModel):
    id: Optional[str] = Field(None, description="Version id within the kind (required on create)")
    kind: str = Field(
        ...,
        description="system | code_exec | distillation | plan_mode | subagents | turbo",
    )
    name: Optional[str] = None
    description: Optional[str] = None
    parts: Optional[List[PromptPartPayload]] = None
    from_id: Optional[str] = Field(None, description="Clone source version id within the same kind")


class KindCreateRequest(BaseModel):
    """新开一个提示词 tab（kind）。"""

    key: str = Field(..., description="标识，只用英文小写/数字/-/_；会被归一", max_length=64)
    label: str = Field("", description="显示名（tab 上的字）", max_length=60)


@router.get("/kinds", dependencies=[Depends(require_config)], summary="提示词 kind（tab）清单")
def list_prompt_kinds(db: Session = Depends(get_db)):
    """内置 + 管理员自建的 kind。「模式选择」页绑定提示词时也读这份清单。"""
    pvs.seed_from_filesystem(db=db)
    return success_response(data={"kinds": pvs.all_kinds(db)})


@router.post("/kinds", dependencies=[Depends(require_config)], summary="新建提示词 tab")
def create_prompt_kind(req: KindCreateRequest, db: Session = Depends(get_db)):
    """新开一个 tab，并给它建好可直接编辑的空 default 版本。"""
    try:
        created = pvs.create_custom_kind(req.key, req.label, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return success_response(data=created)


@router.delete("/kinds/{key}", dependencies=[Depends(require_config)], summary="删除提示词 tab")
def delete_prompt_kind(key: str, db: Session = Depends(get_db)):
    """删掉自定义 tab 及其全部版本；内置 tab 拒绝删除。

    绑了它的对话模式会退回默认提示词装配，不会让对话起不来。
    """
    try:
        ok = pvs.delete_custom_kind(key, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"kind not found: {key}")
    return success_response(data={"deleted": key})


@router.get("/versions", dependencies=[Depends(require_config)], summary="版本池列表")
def list_pool_versions(kind: Optional[str] = None, db: Session = Depends(get_db)):
    """列出提示词版本池中的所有版本，可按 kind 过滤；并返回各 kind 的激活映射。"""
    # Auto-seed defaults on first read so the pool is never empty after boot
    pvs.seed_from_filesystem(db=db)
    items = pvs.list_versions(kind=kind, db=db)
    payload = pvs._load_payload(db)  # noqa: SLF001 — access for active map
    return success_response(
        data={
            "versions": items,
            "active": payload.get("active") or {},
        }
    )


@router.get(
    "/versions/{kind}/{version_id}",
    dependencies=[Depends(require_config)],
    summary="版本池版本详情",
)
def get_pool_version(kind: str, version_id: str, db: Session = Depends(get_db)):
    """获取版本池中指定 kind/version_id 版本的完整内容。"""
    v = pvs.get_version(kind, version_id, db=db)
    if v is None:
        raise HTTPException(status_code=404, detail=f"version not found: {kind}/{version_id}")
    return success_response(data=v)


@router.post("/versions", dependencies=[Depends(require_config)], summary="创建版本池版本")
def create_pool_version(req: VersionUpsertRequest, db: Session = Depends(get_db)):
    """在版本池中新建一个版本（id 必填，可从 from_id 克隆）；版本已存在时返回 409。"""
    if not req.id:
        raise HTTPException(status_code=400, detail="id is required when creating a version")
    # Reject duplicates explicitly
    if pvs.get_version(req.kind, req.id, db=db) is not None:
        raise HTTPException(status_code=409, detail=f"version already exists: {req.kind}/{req.id}")
    parts = [p.model_dump() for p in (req.parts or [])] if req.parts is not None else None
    try:
        saved = pvs.upsert_version(
            req.kind,
            req.id,
            name=req.name,
            description=req.description,
            parts=parts,
            from_id=req.from_id,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return success_response(data=saved)


@router.put(
    "/versions/{kind}/{version_id}",
    dependencies=[Depends(require_config)],
    summary="更新版本池版本",
)
def update_pool_version(
    kind: str,
    version_id: str,
    req: VersionUpsertRequest,
    db: Session = Depends(get_db),
):
    """更新版本池中指定版本的元数据与分段；路径中的 kind/version_id 为准，更新后失效提示词缓存。"""
    # kind/version_id in path are authoritative; body fields override metadata
    if pvs.get_version(kind, version_id, db=db) is None:
        raise HTTPException(status_code=404, detail=f"version not found: {kind}/{version_id}")
    parts = [p.model_dump() for p in (req.parts or [])] if req.parts is not None else None
    saved = pvs.upsert_version(
        kind,
        version_id,
        name=req.name,
        description=req.description,
        parts=parts,
        db=db,
    )
    # Active version edits should bust the prompt cache
    try:
        from prompts.prompt_runtime import invalidate_prompt_cache

        invalidate_prompt_cache()
    except Exception:
        pass
    return success_response(data=saved)


@router.delete(
    "/versions/{kind}/{version_id}",
    dependencies=[Depends(require_config)],
    summary="删除版本池版本",
)
def delete_pool_version(kind: str, version_id: str, db: Session = Depends(get_db)):
    """从版本池中删除指定版本（激活中的版本不可删除）。"""
    try:
        pvs.delete_version(kind, version_id, db=db)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"version not found: {kind}/{version_id}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return success_response(data={"kind": kind, "id": version_id, "message": "deleted"})


@router.post(
    "/versions/{kind}/{version_id}/activate",
    dependencies=[Depends(require_config)],
    summary="激活版本池版本",
)
def activate_pool_version(kind: str, version_id: str, db: Session = Depends(get_db)):
    """将指定版本设为该 kind 的激活版本，并失效提示词缓存使其立即生效。"""
    try:
        pvs.activate_version(kind, version_id, db=db)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"version not found: {kind}/{version_id}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _invalidate_prompt_cache()
    logger.info("prompt_version_activated: %s/%s", kind, version_id)
    return success_response(data={"kind": kind, "id": version_id, "message": "activated"})


@router.post("/versions/seed", dependencies=[Depends(require_config)], summary="初始化版本池")
def seed_pool(db: Session = Depends(get_db)):
    """从文件系统默认提示词初始化版本池。"""
    result = pvs.seed_from_filesystem(db=db)
    _invalidate_prompt_cache()
    return success_response(data=result)


class DesktopPreviewRequest(BaseModel):
    version_id: str
    parts: Optional[List[PromptPartPayload]] = None


@router.post("/desktop-preview")
async def desktop_preview(request: DesktopPreviewRequest, _=Depends(require_config), db: Session = Depends(get_db)):
    """Render a saved desktop version without activating it or launching an agent."""
    from core.config.local_mode import local_mode_enabled
    from prompts.desktop_templates import desktop_version, render_desktop_part
    from prompts.desktop_workspace import SKIP_PARTS
    from prompts.prompt_runtime import build_system_prompt
    version = pvs.get_version("desktop", request.version_id, db=db)
    if version is None:
        raise HTTPException(404, "desktop prompt version not found")
    if request.parts is not None:
        version = {**version, "parts": [p.model_dump() for p in request.parts]}
    local = local_mode_enabled()
    ctx = {"chat_id": "admin_prompt_preview", "approval_mode": "ask"}
    with desktop_version(version):
        if not local:
            # A cloud Config host cannot discover a user's desktop OS or filesystem.
            ctx["preview_environment"] = render_desktop_part("environment", environment_xml=(
                "<environment_context>\n  <cwd>{runtime.cwd}</cwd>\n"
                "  <os>{runtime.os}</os>\n  <os_release>{runtime.os_release}</os_release>\n"
                "  <architecture>{runtime.architecture}</architecture>\n"
                "  <shell>{runtime.shell}</shell>\n  <shell_executable>{runtime.shell_executable}</shell_executable>\n"
                "  <current_date>{runtime.current_date}</current_date>\n  <timezone>{runtime.timezone}</timezone>\n"
                "  <filesystem>{runtime.filesystem}</filesystem>\n</environment_context>"
            )) or " "
        prompt = build_system_prompt(load_prompt_config(), ctx, desktop_preview=True)
        if code_capability_enabled():
            parts = pvs.effective_parts("code_exec", db=db)
            prompt += "\n\n" + "\n\n".join(p["content"] for p in sorted(parts, key=lambda p: p.get("sort_order", 0))
                if p.get("is_enabled", True) and p.get("part_id") not in SKIP_PARTS["code_exec"])
        from core.sandbox._common import WORKSPACE
        from core.sandbox.desktop_paths import workspace_directory
        tool = render_desktop_part("bash_tool", cwd=workspace_directory(WORKSPACE, ctx["chat_id"]) if local else "{runtime.cwd}")
        runtime = await _runtime_prompt_preview(db, approval_mode="ask") if local else None
        if runtime:
            prompt = runtime["prompt"]
        conditional = {p["part_id"]: p.get("content", "") for p in version.get("parts", [])
                       if p.get("is_enabled", True) and p["part_id"] not in {"environment", "guidance", "bash_tool"}}
    return success_response({"prompt": prompt, "bash_tool": tool, "conditional_parts": conditional,
        "environment_source": "local_host" if local else "runtime_placeholders", "version_id": version["id"],
        "preview_mode": "runtime" if runtime else "template"})


@router.get("/snapshot", dependencies=[Depends(require_config)])
def export_snapshot(db: Session = Depends(get_db)):
    from core.content.content_blocks import build_prompt_snapshot
    return success_response(build_prompt_snapshot(db))


@router.post("/snapshot", dependencies=[Depends(require_config)])
def import_snapshot(body: Dict[str, Any], db: Session = Depends(get_db)):
    from core.content.content_blocks import import_prompt_snapshot, ContentSnapshotError
    try:
        result = import_prompt_snapshot(db, body, overwrite=True, default_updated_by="prompt_management")
    except ContentSnapshotError as exc:
        raise HTTPException(400, str(exc)) from exc
    pvs.invalidate_cache()
    _invalidate_prompt_cache()
    return success_response(result)
