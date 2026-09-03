"""Unified capability resolution logic.

Centralises the merging of catalog.json defaults, public DB capabilities,
and per-user DB overrides so that every consumer (chat endpoint, factory,
workflow, subagents) uses the same algorithm.
"""

from __future__ import annotations

import logging
from threading import Lock
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.config.catalog import get_enabled_ids, is_enabled
from core.config.catalog_runtime import get_runtime_catalog

logger = logging.getLogger(__name__)

# ── Per-user capability cache ────────────────────────────────────────────
_CAPABILITY_CACHE_TTL = 30.0  # seconds
_capability_cache_lock = Lock()
# user_id -> (expires_at, (skills, agents, mcps))
_capability_cache: Dict[
    str, Tuple[float, Tuple[Optional[List[str]], Optional[List[str]], Optional[List[str]]]]
] = {}


# ── Context helpers (extract typed lists from a runtime context dict) ────────


def _extract_ids_from_context(context: Dict[str, Any], key: str) -> Optional[List[str]]:
    """Extract a list of non-empty string IDs from *context[key]*.

    Returns ``None`` if the key is absent or not a list, allowing callers
    to distinguish "not provided" from "empty list".
    """
    raw = context.get(key)
    if not isinstance(raw, list):
        return None
    return [str(item).strip() for item in raw if str(item).strip()]


def enabled_skill_ids_from_context(context: Dict[str, Any]) -> Optional[List[str]]:
    return _extract_ids_from_context(context, "enabled_skills")


def enabled_agent_ids_from_context(context: Dict[str, Any]) -> Optional[List[str]]:
    return _extract_ids_from_context(context, "enabled_agents")


def enabled_mcp_ids_from_context(context: Dict[str, Any]) -> Optional[List[str]]:
    return _extract_ids_from_context(context, "enabled_mcps")


def enabled_kb_ids_from_context(context: Dict[str, Any]) -> Optional[List[str]]:
    return _extract_ids_from_context(context, "enabled_kbs")


def is_agent_route_enabled(route: str, context: Dict[str, Any]) -> bool:
    """Check whether a given agent/sub-agent route is enabled for this request."""
    ids = enabled_agent_ids_from_context(context)
    if isinstance(ids, list):
        return route in set(ids)
    return is_enabled("agents", route)


# ── Full resolution (static catalog + public DB capabilities + overrides) ───


def _merge_kind(
    base_items: list,
    user_overrides: list,
) -> List[str]:
    """Merge base catalog items with user overrides, return sorted enabled IDs.

    Only IDs present in base_items are eligible; user_overrides can only
    flip the enabled flag for existing items, never resurrect deleted ones.

    **Admin lock rule**: if an item is disabled in the base catalog
    (catalog.json ``enabled=false``), user overrides CANNOT re-enable it.
    This ensures admin-disabled capabilities are truly unavailable.
    """
    # Build base map: id -> enabled
    base_map: Dict[str, bool] = {}
    for item in base_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        if item_id:
            base_map[item_id] = bool(item.get("enabled", True))

    enabled_map = dict(base_map)
    for item in user_overrides:
        if isinstance(item, dict):
            item_id = str(item.get("id", "")).strip()
            # Only update existing IDs — do not re-add items deleted from catalog.
            # Admin-disabled items (base enabled=false) are locked — user cannot re-enable.
            if item_id and item_id in enabled_map and base_map.get(item_id, True):
                enabled_map[item_id] = bool(item.get("enabled", False))
    return sorted(k for k, v in enabled_map.items() if v)


def _owned_enabled_ids(
    db: Session,
    user_id: str,
    overrides: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    """The enabled ids among the user's **own private skills / MCPs** (owner_user_id == user_id).

    Private / skill-marketplace-installed capabilities are not in the global
    catalog (excluded by ``_private_skill_ids``), so ``_merge_kind`` (which only
    recognizes ids from the global base catalog) can never reach them — meaning
    a user's activated private skills / MCPs would not get injected into the
    agent toolbox and system prompt. This function fills that gap: it computes
    effective-enabled by "override first, otherwise AdminSkill.is_enabled" (same
    semantics as the /v1/catalog display and the PATCH that writes overrides),
    returning (owned_skill_ids, owned_mcp_ids).

    Multi-tenant safety: only rows with ``owner_user_id == user_id`` are taken,
    never leaking into another user.
    """
    from core.db.models import AdminMcpServer, AdminSkill

    def _ov_map(key: str) -> Dict[str, bool]:
        return {
            str(o["id"]).strip(): bool(o.get("enabled", False))
            for o in (overrides.get(key) or [])
            if isinstance(o, dict) and str(o.get("id", "")).strip()
        }

    def _collect(model, id_col, label: str, *, dep_gated: bool = False) -> List[str]:
        # Only fetch the (id, is_enabled) two columns — large fields like AdminSkill.skill_content / extra_files need not be hydrated.
        ov = _ov_map(label)
        out: List[str] = []
        try:
            q = db.query(id_col, model.is_enabled).filter(model.owner_user_id == user_id)
            if dep_gated:
                # Skills whose dependencies are not yet installed (dep_status='installing') are soft-disabled: not loaded at runtime, to avoid the script
                # erroring midway due to a missing package. It is still visible on the capability center / skills page marked "installing", and auto-recovers once installed.
                q = q.filter(model.dep_status == "ready")
            rows = q.all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("owned %s resolve failed user=%s: %s", label, user_id, exc)
            return out
        for rid, is_enabled in rows:
            rid = str(rid or "").strip()
            if rid and ov.get(rid, bool(is_enabled)):
                out.append(rid)
        return out

    return (
        _collect(AdminSkill, AdminSkill.skill_id, "skills", dep_gated=True),
        _collect(AdminMcpServer, AdminMcpServer.server_id, "mcps"),
    )


def resolve_explicit_runtime_capabilities(
    db: Session,
    user_id: str,
    *,
    skill_ids: Optional[List[str]] = None,
    mcp_ids: Optional[List[str]] = None,
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """Resolve capabilities that may be summoned explicitly for one turn.

    A user's catalog override controls default assembly and discovery, not an
    explicit ``/`` or ``+`` selection. The global/admin state remains a hard
    boundary: disabled or dependency-blocked public capabilities cannot be
    resurrected, and private capabilities are eligible only for their owner.

    Returns ``(allowed_skills, allowed_mcps, unavailable_skills,
    unavailable_mcps)`` while preserving request order.
    """

    def _clean(raw: Optional[List[str]]) -> List[str]:
        return list(
            dict.fromkeys(
                str(item).strip()
                for item in (raw or [])
                if isinstance(item, str) and item.strip()
            )
        )

    requested_skills = _clean(skill_ids)
    requested_mcps = _clean(mcp_ids)
    if not requested_skills and not requested_mcps:
        return [], [], [], []

    base_catalog = get_runtime_catalog(db, include_runtime_details=False)
    allowed_skill_ids = {
        str(item.get("id") or "").strip()
        for item in (base_catalog.get("skills") or [])
        if isinstance(item, dict) and bool(item.get("enabled", True))
    }
    allowed_mcp_ids = {
        str(item.get("id") or "").strip()
        for item in (base_catalog.get("mcp") or [])
        if isinstance(item, dict) and bool(item.get("enabled", True))
    }

    # Private capabilities are deliberately absent from the global runtime
    # catalog. Their persisted global/readiness state is still authoritative;
    # only the per-user override is ignored for this explicit turn.
    from core.db.models import AdminMcpServer, AdminSkill

    unresolved_skills = [sid for sid in requested_skills if sid not in allowed_skill_ids]
    if unresolved_skills:
        owned_rows = (
            db.query(AdminSkill.skill_id)
            .filter(
                AdminSkill.skill_id.in_(unresolved_skills),
                AdminSkill.owner_user_id == user_id,
                AdminSkill.is_enabled.is_(True),
                AdminSkill.dep_status == "ready",
            )
            .all()
        )
        allowed_skill_ids.update(str(row[0]) for row in owned_rows if row[0])

    unresolved_mcps = [mid for mid in requested_mcps if mid not in allowed_mcp_ids]
    if unresolved_mcps:
        owned_rows = (
            db.query(AdminMcpServer.server_id)
            .filter(
                AdminMcpServer.server_id.in_(unresolved_mcps),
                AdminMcpServer.owner_user_id == user_id,
                AdminMcpServer.is_enabled.is_(True),
            )
            .all()
        )
        allowed_mcp_ids.update(str(row[0]) for row in owned_rows if row[0])

    allowed_skills = [sid for sid in requested_skills if sid in allowed_skill_ids]
    allowed_mcps = [mid for mid in requested_mcps if mid in allowed_mcp_ids]
    unavailable_skills = [sid for sid in requested_skills if sid not in allowed_skill_ids]
    unavailable_mcps = [mid for mid in requested_mcps if mid not in allowed_mcp_ids]
    return allowed_skills, allowed_mcps, unavailable_skills, unavailable_mcps


def _apply_desktop_cloud_bridge(result):
    """双端桌面本机后端：把云端授权 MCP 合并进 enabled_mcps（含本机同基名抑制）。

    这里是 enabled 能力解析的单一真源——主对话、定时任务、批量子代理、作业
    唤醒等全部装配路径共用，桥合并在此做一次即全链路生效。故意应用在缓存
    **之外**：manifest 有自己的刷新节奏，不随本缓存冻结。云端部署 / 纯本机
    模式下桥未激活，原样返回（零行为变化）。
    """
    skills, agents, mcps = result
    if mcps is None:
        return result
    try:
        from core.services.desktop_cloud_bridge import apply_to_enabled_mcp_ids

        bridged = apply_to_enabled_mcp_ids(list(mcps))
    except Exception:  # noqa: BLE001 - 桥故障不能影响能力解析
        return result
    return (skills, agents, bridged)


def resolve_all_runtime_enabled(
    db: Session,
    user_id: str,
) -> Tuple[Optional[List[str]], Optional[List[str]], Optional[List[str]]]:
    """Resolve user-effective enabled skills, agents, and MCPs in one pass.

    Results are cached per user_id for 30 seconds to avoid repeated DB
    queries when the same user sends multiple messages in quick succession.

    Loads static catalog + public DB capabilities and ``CatalogService`` once,
    then merges base defaults with per-user overrides.

    Returns ``(enabled_skills, enabled_agents, enabled_mcps)``.
    On error, returns ``(None, None, None)`` so callers fall back to
    static catalog defaults.
    """
    # Check cache first
    now = monotonic()
    with _capability_cache_lock:
        cached = _capability_cache.get(user_id)
        if cached is not None:
            expires_at, result = cached
            if now < expires_at:
                return _apply_desktop_cloud_bridge(result)
            else:
                _capability_cache.pop(user_id, None)

    try:
        # Lazy import to avoid circular dependency at module level
        from core.services import CatalogService

        base_catalog = get_runtime_catalog(db, include_runtime_details=False)
        svc = CatalogService(db)
        overrides = svc.get_user_overrides(user_id)

        skills = _merge_kind(
            base_catalog.get("skills") or [],
            overrides.get("skills", []),
        )
        agents = _merge_kind(
            base_catalog.get("agents") or [],
            overrides.get("agents", []),
        )
        mcps = _merge_kind(
            base_catalog.get("mcp") or [],
            overrides.get("mcps", []),
        )
        # Merge in the user's own activated private / skill-marketplace-installed skills / MCPs (not in the
        # global catalog, unreachable by _merge_kind) — so each user's agent toolbox / system prompt varies by their activated capabilities.
        owned_skills, owned_mcps = _owned_enabled_ids(db, user_id, overrides)
        # Dedup: if a privately-installed skill / MCP has already been set global by an admin (same underlying entry_name), drop the private
        # copy — otherwise the same capability would be loaded twice (global id=entry, private id=entry-fingerprint), which is wasteful and also lets
        # the model see two identically-named, identical-content tools/skills. Same semantics as the dedup on the /v1/catalog display side.
        from core.services.marketplace_service import base_entry_name

        if owned_skills:
            _global_skills = set(skills)
            owned_skills = [
                sid for sid in owned_skills if base_entry_name(sid, user_id) not in _global_skills
            ]
            if owned_skills:
                skills = sorted(_global_skills | set(owned_skills))
        if owned_mcps:
            _global_mcps = set(mcps)
            owned_mcps = [
                mid for mid in owned_mcps if base_entry_name(mid, user_id) not in _global_mcps
            ]
            if owned_mcps:
                mcps = sorted(_global_mcps | set(owned_mcps))
        result = (skills, agents, mcps)

        # Store in cache
        with _capability_cache_lock:
            _capability_cache[user_id] = (now + _CAPABILITY_CACHE_TTL, result)

        return _apply_desktop_cloud_bridge(result)
    except Exception as exc:
        logger.warning("resolve_all_runtime_enabled failed: %s (user=%s)", exc, user_id)
        return None, None, None


def invalidate_capability_cache(user_id: Optional[str] = None) -> None:
    """Invalidate capability cache. Pass user_id to clear a specific user, or None for all."""
    with _capability_cache_lock:
        if user_id is None:
            _capability_cache.clear()
        else:
            _capability_cache.pop(user_id, None)
