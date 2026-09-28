"""Catalog management API routes (v1)."""

import asyncio
import logging
from copy import deepcopy
from threading import Lock
from time import monotonic
from typing import Any, Dict, List, Optional

from core.auth.backend import UserContext, get_current_user
from core.config.catalog_runtime import get_runtime_catalog
from core.config.settings import settings
from core.db.engine import get_db
from core.db.repository import KBRepository
from core.infra.exceptions import BadRequestError
from core.infra.responses import success_response
from core.kb.external_provider import (
    get_provider_cache_identity,
    get_provider_name,
    is_enabled,
    list_collections,
)
from core.services import CatalogService
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

# ── External knowledge collection cache ──
_external_cache_lock = Lock()
_external_cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
_external_cache_refreshing: set[str] = set()
_external_cache_async_lock: Optional[asyncio.Lock] = None
_external_cache_async_lock_loop: Optional[asyncio.AbstractEventLoop] = None
_EXTERNAL_CACHE_TTL = 30.0
_EXTERNAL_CACHE_FAILURE_TTL = 5.0
_EXTERNAL_CACHE_CONTEXT_RETRIES = 2


def _external_cache_context() -> tuple[str, str]:
    provider = get_provider_name() or "dify"
    return provider, get_provider_cache_identity(provider)


def _retry_after_cache_context_change(
    cache_identity: str,
    context_retry: int,
) -> List[Dict[str, Any]]:
    with _external_cache_lock:
        _external_cache_refreshing.discard(cache_identity)
    if context_retry >= _EXTERNAL_CACHE_CONTEXT_RETRIES:
        raise RuntimeError("Knowledge provider configuration changed repeatedly during refresh")
    return _list_datasets_cached(_context_retry=context_retry + 1)


def _prune_external_cache_locked(now: float, preserve: str) -> None:
    expired = [
        identity
        for identity, (expires_at, _) in _external_cache.items()
        if identity != preserve and expires_at <= now and identity not in _external_cache_refreshing
    ]
    for identity in expired:
        del _external_cache[identity]


def _list_datasets_cached(*, _context_retry: int = 0) -> List[Dict[str, Any]]:
    """Cache collections by provider config and discard in-flight stale snapshots."""
    provider, cache_identity = _external_cache_context()
    now = monotonic()
    with _external_cache_lock:
        _prune_external_cache_locked(now, cache_identity)
        cached = _external_cache.get(cache_identity)
        if cached is not None:
            expires_at, items = cached
            if now < expires_at:
                return deepcopy(items)

        stale_items = cached[1] if cached is not None else None
        if cache_identity in _external_cache_refreshing:
            if stale_items is not None:
                return deepcopy(stale_items)
            # Async route callers wait for the shared cold refresh before reaching here.
            return []

        _external_cache_refreshing.add(cache_identity)

    try:
        items = list_collections(
            page=1,
            limit=100,
            raise_on_error=True,
            provider_name=provider,
        )
        cache_items = deepcopy(items)
    except Exception as exc:
        if _external_cache_context() != (provider, cache_identity):
            return _retry_after_cache_context_change(cache_identity, _context_retry)
        fallback_items = deepcopy(stale_items) if stale_items is not None else []
        with _external_cache_lock:
            _external_cache[cache_identity] = (
                monotonic() + _EXTERNAL_CACHE_FAILURE_TTL,
                deepcopy(fallback_items),
            )
            _external_cache_refreshing.discard(cache_identity)
        if stale_items is not None:
            logger.warning("External catalog refresh failed; serving stale cache: %s", exc)
            return fallback_items
        raise

    if _external_cache_context() != (provider, cache_identity):
        return _retry_after_cache_context_change(cache_identity, _context_retry)

    with _external_cache_lock:
        _external_cache[cache_identity] = (monotonic() + _EXTERNAL_CACHE_TTL, cache_items)
        _external_cache_refreshing.discard(cache_identity)
    return deepcopy(cache_items)


def _external_cache_is_fresh() -> bool:
    _, cache_identity = _external_cache_context()
    with _external_cache_lock:
        cached = _external_cache.get(cache_identity)
        return cached is not None and monotonic() < cached[0]


def _get_external_cache_async_lock() -> asyncio.Lock:
    """Return a refresh lock bound to the active Uvicorn event loop."""
    global _external_cache_async_lock, _external_cache_async_lock_loop

    loop = asyncio.get_running_loop()
    with _external_cache_lock:
        if _external_cache_async_lock is None or _external_cache_async_lock_loop is not loop:
            _external_cache_async_lock = asyncio.Lock()
            _external_cache_async_lock_loop = loop
        return _external_cache_async_lock


def _prepare_external_cache_sync() -> None:
    if settings.edition.edition != "ce" and is_enabled():
        _list_datasets_cached()


async def _ensure_external_cache_ready() -> None:
    """Share a cold refresh without parking follower requests in worker threads."""
    if _external_cache_is_fresh():
        return

    async with _get_external_cache_async_lock():
        if _external_cache_is_fresh():
            return
        try:
            await run_in_threadpool(_prepare_external_cache_sync)
        except Exception as exc:
            # The synchronous helper installs a short failure cache before raising.
            logger.warning("Failed to prepare external knowledge cache: %s", exc)


router = APIRouter(prefix="/v1/catalog", tags=["Catalog"])
logger = logging.getLogger(__name__)


def _index_modes_of(space) -> List[str]:
    """自建知识库的索引模式；解析失败按仅 RAG（历史库的语义）。"""
    try:
        from core.kb.wiki.config import index_modes_of

        return index_modes_of(space)
    except Exception:  # noqa: BLE001
        return ["rag"]


def _kb_capabilities(space) -> Dict[str, bool]:
    """索引模式 → 能力位，字段与外接知识库的 capabilities 同构。"""
    try:
        from core.kb.wiki_router import capabilities_for_local_kb

        return capabilities_for_local_kb(space)
    except Exception:  # noqa: BLE001
        return {"vector": True, "keyword": True, "wiki": False, "graph": False}


from .catalog_ownership import _load_owned_capability_items, _plugin_component_ids, _is_owned_capability


def _get_catalog_items_sync(user: UserContext, db: Session):
    """获取完整能力目录，含技能（skills）、子智能体（agents）、MCP 服务及知识库（KB）。

    在系统默认配置的基础上叠加当前用户的个性化覆盖：每项的启用状态与配置优先返回
    用户自定义值，否则用默认值；管理员在 catalog.json 中禁用的项对前台完全隐藏。
    EE 的 KB 包含公共库与当前用户的私有库；CE 只返回当前用户的私有库。
    """
    # Opportunistically clear this user's capability-resolution cache: on the agent side,
    # resolve_all_runtime_enabled has a 30s in-process cache, and it is cross-process (the
    # mcp container changing the DB cannot touch the backend process's cache). The frontend
    # re-fetches this endpoint after a skill is created/installed/deleted (including when
    # the agent triggers it via the skill-manager plugin's MCP tools) — piggyback on this
    # request within the backend process to clear the cache, so a "just-created skill"
    # takes effect for the agent on the very next message, avoiding up to 30s of it being
    # invisible/unusable. The catalog itself queries the DB directly and does not use this
    # cache, so this call only affects agent resolution and is harmless to display.
    try:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache(str(user.user_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalog GET: invalidate_capability_cache failed: %s", exc)

    # Static catalog.json + public DB-managed capabilities.
    base_catalog = get_runtime_catalog(db)

    # Get user overrides (graceful degradation if table doesn't exist yet)
    try:
        catalog_service = CatalogService(db)
        user_overrides = catalog_service.get_user_overrides(user.user_id)
    except Exception as exc:
        logger.warning("Failed to load user catalog overrides: %s", exc)
        user_overrides = {"skills": [], "agents": [], "mcps": []}

    # Merge base catalog with user overrides
    def merge_items(base_items: List[Dict], override_items: List[Dict]) -> List[Dict]:
        """Merge base catalog items with user overrides.

        Admin lock rule: if an item is disabled in the base catalog
        (catalog.json enabled=false), user overrides cannot re-enable it.
        """
        # Create a map of overrides by id
        override_map = {item["id"]: item for item in override_items}

        result = []
        for base_item in base_items:
            item_id = base_item.get("id")
            base_enabled = bool(base_item.get("enabled", True))

            # Admin-disabled items are completely hidden from user frontend
            if not base_enabled:
                continue

            # Start with base item
            merged = dict(base_item)

            # Apply user override if exists
            if item_id in override_map:
                override = override_map[item_id]
                merged["enabled"] = override["enabled"]
                if override.get("config"):
                    # Merge configs
                    base_config = merged.get("config", {})
                    override_config = override.get("config", {})
                    merged["config"] = {**base_config, **override_config}

            config = merged.get("config", {})
            if isinstance(config, dict):
                tags = config.get("tags")
                if isinstance(tags, list):
                    merged["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]
                server = config.get("server")
                if isinstance(server, str) and server.strip():
                    merged["server"] = server.strip()

            result.append(merged)

        return result

    # ── Private skills / MCPs self-added by the current user (owner-isolated, not in the global catalog) ──
    owned_skill_items, owned_mcp_items = _load_owned_capability_items(db, user.user_id)

    # Dedupe: if a user's privately installed skill / MCP has been promoted to global by
    # an admin (same underlying entry_name), hide the private duplicate and keep only the
    # global version — otherwise the capability library would show two identically named,
    # identical-content items (private id=entry-fingerprint, global id=entry, which differ
    # so both entered the list). Merge by entry_name; dedupe only against enabled global items.
    from core.services.marketplace_service import base_entry_name

    for kind, owned in (("skills", owned_skill_items), ("mcp", owned_mcp_items)):
        global_ids = {it.get("id") for it in base_catalog.get(kind, []) if it.get("enabled", True)}
        owned[:] = [it for it in owned if base_entry_name(it["id"], user.user_id) not in global_ids]

    # Merge each category
    mcp_items = merge_items(
        base_catalog.get("mcp", []) + owned_mcp_items, user_overrides.get("mcps", [])
    )

    is_ce = settings.edition.edition == "ce"

    # ── Externally managed public knowledge collections ───────────────────────
    public_kb_items: List[Dict[str, Any]] = []
    if not is_ce and is_enabled():
        try:
            external_items = _list_datasets_cached()
            # Permission assignment: narrow to datasets visible to the current user (public + granted scoped; defaults to public when unset).
            from core.auth.kb_permissions import get_dataset_levels

            ds_ids = [str(item.get("id", "")).strip() for item in external_items if item.get("id")]
            ds_levels = get_dataset_levels(db, user.user_id, ds_ids)
            for item in external_items:
                ds_id = str(item.get("id", "")).strip()
                level = ds_levels.get(ds_id)
                if not level:
                    continue
                item["visibility"] = "public"
                item["is_public"] = True
                item["access_level"] = level
                public_kb_items.append(item)
        except Exception as exc:
            logger.warning("Failed to load external knowledge collections: %s", exc)

    # ── Private KB (local Milvus) ─────────────────────────────────────────────
    try:
        kb_repo = KBRepository(db)
        spaces = kb_repo.list_spaces(user.user_id)
    except Exception as exc:
        logger.warning("Failed to load private KB spaces: %s", exc)
        spaces = []
    private_kb_items: List[Dict[str, Any]] = []
    for space in spaces:
        if is_ce and space.visibility != "private":
            continue
        extra = space.extra_data if isinstance(space.extra_data, dict) else {}
        is_system_managed = bool(extra.get("system_managed"))
        if is_system_managed:
            continue
        tag = str(extra.get("tag") or "").strip()
        tags = [tag] if tag else []
        modes = _index_modes_of(space)
        capabilities = _kb_capabilities(space)
        if capabilities.get("wiki"):
            tags = [*tags, "LLM Wiki"]
        private_kb_items.append(
            {
                "id": space.kb_id,
                "kind": "knowledge_base",
                "name": space.name,
                "description": space.description or "无简介",
                "desc": space.description or "无简介",
                "enabled": True,
                "version": "local",
                "provider": "HugAgentOS-KB",
                "visibility": space.visibility,
                "is_public": space.visibility == "public",
                "chunk_method": space.chunk_method or "semantic",
                "index_modes": modes,
                # 前端据此决定是否渲染 Wiki / 图谱入口，与外接知识库的字段同构
                "capabilities": capabilities,
                "document_count": space.document_count or 0,
                "total_size_bytes": space.total_size_bytes or 0,
                "detail": (f"### {space.name}\n\n" f"{space.description or '暂无简介'}\n"),
                "tags": tags,
                "system_managed": is_system_managed,
                "pinned": bool(extra.get("pinned")),
                "editable": not is_system_managed and extra.get("editable", True) is not False,
                "deletable": not is_system_managed and extra.get("deletable", True) is not False,
                "uploadable": not is_system_managed and extra.get("uploadable", True) is not False,
            }
        )
    private_kb_items.sort(key=lambda item: (not bool(item.get("pinned")), item.get("name", "")))

    # ── Shared KB (admin-managed local Milvus: public = everyone / scoped = specified visibility) ──
    # Single source of truth for permission assignment: show only shared spaces the current
    # user may access (public spaces + granted scoped spaces), excluding ones they own
    # (already in private_kb_items). Capability bits follow the grant level: view =
    # read-only, edit = can upload, admin = can manage. On the retrieval side, the
    # public/scoped branches of retrieve_local_kb admit the same visible set.
    public_local_kb_items: List[Dict[str, Any]] = []
    if not is_ce:
        try:
            from core.auth.kb_permissions import get_accessible_local_kb_levels, level_to_caps
            from core.db.models import KBSpace

            levels = get_accessible_local_kb_levels(db, user.user_id)
            owned_ids = {s.kb_id for s in spaces}
            shared_ids = [kid for kid in levels if kid not in owned_ids]
            shared_spaces = []
            if shared_ids:
                shared_spaces = (
                    db.query(KBSpace)
                    .filter(
                        KBSpace.kb_id.in_(shared_ids),
                        KBSpace.deleted_at.is_(None),
                    )
                    .all()
                )
            for space in shared_spaces:
                extra = space.extra_data if isinstance(space.extra_data, dict) else {}
                if bool(extra.get("system_managed")):
                    continue
                level = levels.get(space.kb_id, "view")
                # Keep only the admin-defined tag; visibility grants are not exposed as labels.
                tag = str(extra.get("tag") or "").strip()
                shared_tags = [tag] if tag else []
                shared_caps = _kb_capabilities(space)
                if shared_caps.get("wiki"):
                    shared_tags = [*shared_tags, "LLM Wiki"]
                public_local_kb_items.append(
                    {
                        "id": space.kb_id,
                        "kind": "knowledge_base",
                        "name": space.name,
                        "description": space.description or "无简介",
                        "desc": space.description or "无简介",
                        "enabled": True,
                        "version": "local",
                        "provider": "HugAgentOS-KB",
                        "visibility": space.visibility,
                        "is_public": space.visibility == "public",
                        "access_level": level,
                        "chunk_method": space.chunk_method or "semantic",
                        "index_modes": _index_modes_of(space),
                        "capabilities": shared_caps,
                        "document_count": space.document_count or 0,
                        "total_size_bytes": space.total_size_bytes or 0,
                        "detail": (f"### {space.name}\n\n" f"{space.description or '暂无简介'}\n"),
                        "tags": shared_tags,
                        "system_managed": False,
                        "pinned": False,
                        **level_to_caps(level),
                    }
                )
        except Exception as exc:
            logger.warning("Failed to load shared KB spaces: %s", exc)
    public_local_kb_items.sort(key=lambda item: item.get("name", ""))

    # 显式标注来源，别让前端靠 kb_id 前缀猜。前缀是存储细节，不该成为跨端契约；
    # 能力位（capabilities.wiki 等）缺省视为「不具备」，而不是按来源反推。
    for item in public_kb_items:
        item.setdefault("source", "external")
        item.setdefault("capabilities", {})
    for item in public_local_kb_items + private_kb_items:
        item["source"] = "local"

    kb_items: List[Dict[str, Any]] = public_kb_items + public_local_kb_items + private_kb_items

    # 桌面双端的本机后端：云端账号的技能与连接器同步在本机登记表里，业务库中没有
    # 对应行。不在这里叠加进来，能力中心就只剩本机自带的内置项——显示的和真正生效的
    # 不是同一份。云端部署下叠加层为空，这几行是恒等变换。
    from core.capabilities import device_catalog

    overlay = device_catalog.catalog_overlay(user_id=str(user.user_id))
    skill_list = device_catalog.merge_items(
        merge_items(
            base_catalog.get("skills", []) + owned_skill_items, user_overrides.get("skills", [])
        ),
        overlay["skills"],
    )
    mcp_list = device_catalog.merge_items(mcp_items, overlay["mcp"])

    # Plugin components are shown only under "Plugins"; remove them from the skill / MCP tool library lists (avoids duplication; does not affect enablement resolution).
    # 三个来源合起来才完整：业务库里的归属、内置插件包扫描、以及本机登记表里云端
    # 同步来的那部分。剔除放在叠加之后，两边的条目走同一道口径。
    plugin_skill_ids, plugin_mcp_ids = _plugin_component_ids(db)
    plugin_skill_ids |= overlay["hidden_skills"]
    plugin_mcp_ids |= overlay["hidden_mcp"]
    skill_list = [it for it in skill_list if it.get("id") not in plugin_skill_ids]
    mcp_list = [it for it in mcp_list if it.get("id") not in plugin_mcp_ids]

    data = {
        "skills": skill_list,
        "agents": merge_items(base_catalog.get("agents", []), user_overrides.get("agents", [])),
        "mcp": mcp_list,
        "kb": kb_items,
    }

    return success_response(data=data, message="Catalog retrieved successfully")


@router.get("", summary="获取能力目录")
async def get_catalog_items(
    user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Build the synchronous catalog outside the Uvicorn event loop."""
    await _ensure_external_cache_ready()
    return await run_in_threadpool(_get_catalog_items_sync, user, db)



from .catalog_mutations import router as mutations_router
router.include_router(mutations_router)
