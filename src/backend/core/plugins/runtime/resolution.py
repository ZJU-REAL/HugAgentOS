"""Progressive plugin runtime: resolution."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


from core.plugins.runtime import activation_history as plugin_activation_history
from core.plugins.runtime import models as plugin_runtime_models


def _http_transport(cfg: Any) -> bool:
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("url")) or cfg.get("transport") in ("streamable_http", "sse")


def _server_configs(user_id: Optional[str] = None) -> Dict[str, Any]:
    """Transport lookup: global servers, plus the user's private ones when given."""
    try:
        from core.services.mcp_service import McpServerConfigService

        svc = McpServerConfigService.get_instance()
        cfgs = dict(svc.get_all_servers(enabled_only=True))
        if user_id:
            try:
                cfgs.update(svc.get_owned_servers(str(user_id), enabled_only=False))
            except Exception:  # noqa: BLE001
                pass
        return cfgs
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] server config lookup failed: %s", exc)
        return {}


def _skill_metadata() -> Dict[str, Any]:
    try:
        from core.agent_skills.loader import get_skill_loader

        return get_skill_loader().load_all_metadata() or {}
    except Exception:  # noqa: BLE001
        return {}


def _bound_mcp_ids(
    skill_ids: Sequence[str], skill_meta: Dict[str, Any], in_play_mcps: Sequence[str]
) -> List[str]:
    """MCP servers a plugin's skills declare via SKILL.md frontmatter."""
    bound: List[str] = []
    for sid in skill_ids:
        item = skill_meta.get(sid)
        for server_id in getattr(item, "mcp_server_ids", None) or []:
            if server_id and server_id not in bound and server_id not in in_play_mcps:
                bound.append(server_id)
    return bound


def _finalize_resolution(
    res: plugin_runtime_models.ProgressiveResolution,
    eligible: Sequence[plugin_runtime_models.DeferredPlugin],
) -> plugin_runtime_models.ProgressiveResolution:
    """Directory order plus the component ids actually withheld from assembly.

    A component that a plugin staying eager also carries must NOT be withheld:
    subtracting it would make the run require an activation to reach a
    capability it was already entitled to use.
    """
    res.directory = sorted(eligible, key=lambda p: p.slug)
    deferred_ids = {p.install_id for p in res.deferred}
    eager = [p for p in eligible if p.install_id not in deferred_ids]
    res.deferred_skill_ids = {s for p in res.deferred for s in p.skill_ids} - {
        s for p in eager for s in p.skill_ids
    }
    res.deferred_mcp_ids = {m for p in res.deferred for m in p.mcp_ids} - {
        m for p in eager for m in p.mcp_ids
    }
    return res


def resolve_progressive_plugins(
    *,
    user_id: str,
    chat_id: Optional[str],
    enabled_skill_ids: Sequence[str],
    enabled_mcp_ids: Sequence[str],
    invoked_skill_ids: Optional[Sequence[str]] = None,
    invoked_mcp_ids: Optional[Sequence[str]] = None,
) -> plugin_runtime_models.ProgressiveResolution:
    """Decide which installed plugins this run defers.

    Runs in a worker thread (sync DB access). A plugin is deferral-eligible
    when it is visible to the user, has at least one component in this run's
    enabled sets, and none of its in-play MCP servers use stdio transport.
    Eligible plugins already activated for this chat — or explicitly invoked
    this turn — stay eager; explicit invocation is persisted as activation so
    the plugin stays loaded on subsequent turns.
    """
    from core.db.engine import SessionLocal
    from core.db.models import InstalledPlugin
    from sqlalchemy import or_

    enabled_skills = {s for s in enabled_skill_ids if isinstance(s, str) and s.strip()}
    enabled_mcps = {m for m in enabled_mcp_ids if isinstance(m, str) and m.strip()}
    invoked = {x for x in (invoked_skill_ids or []) if isinstance(x, str) and x.strip()}
    invoked |= {x for x in (invoked_mcp_ids or []) if isinstance(x, str) and x.strip()}

    server_cfgs = _server_configs(user_id)
    skill_meta = _skill_metadata()

    activated = plugin_activation_history.load_activated_plugin_slugs(chat_id)
    activated_set = set(activated)

    res = plugin_runtime_models.ProgressiveResolution()
    eligible: List[plugin_runtime_models.DeferredPlugin] = []
    newly_pinned: List[str] = []
    try:
        with SessionLocal() as db:
            rows = (
                db.query(InstalledPlugin)
                .filter(
                    or_(
                        InstalledPlugin.owner_user_id == user_id,
                        InstalledPlugin.owner_user_id.is_(None),
                    )
                )
                .all()
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] installed plugin load failed: %s", exc)
        return res

    for r in rows:
        cids = r.component_ids or {}
        in_play_skills = [s for s in (cids.get("skills") or []) if s in enabled_skills]
        in_play_mcps = [m for m in (cids.get("mcp") or []) if m in enabled_mcps]
        if not in_play_skills and not in_play_mcps:
            continue
        # stdio-transport components keep the whole plugin eager: spawning
        # per-request subprocesses mid-run has lifecycle costs this v1 skips.
        if any(not _http_transport(server_cfgs.get(m)) for m in in_play_mcps):
            continue
        plugin = plugin_runtime_models.DeferredPlugin(
            install_id=str(r.install_id),
            slug=str(r.slug),
            name=str(r.name or r.slug),
            description=str(r.description or ""),
            skill_ids=in_play_skills,
            mcp_ids=in_play_mcps,
            bound_mcp_ids=_bound_mcp_ids(in_play_skills, skill_meta, in_play_mcps),
        )
        eligible.append(plugin)

        components = set(in_play_skills) | set(in_play_mcps)
        if plugin.install_id in activated_set or plugin.slug in activated_set:
            if plugin.slug not in res.activated_slugs:
                res.activated_slugs.append(plugin.slug)
            continue
        if invoked & components:
            # Explicit invocation this turn = activation; persist below so the
            # next turn keeps the plugin eager without re-invocation.
            newly_pinned.append(plugin.install_id)
            if plugin.slug not in res.activated_slugs:
                res.activated_slugs.append(plugin.slug)
            continue
        res.deferred.append(plugin)

    if newly_pinned:
        plugin_activation_history.record_plugin_activation(chat_id, newly_pinned)

    return _finalize_resolution(res, eligible)


def resolve_bound_progressive_plugins(
    install_ids: Sequence[str],
    *,
    skill_filter: Optional[Any] = None,
) -> plugin_runtime_models.ProgressiveResolution:
    """Deferral resolution for a sub-agent's explicitly bound plugins.

    Differences from the main-path resolver: plugins are looked up by
    ``install_id`` (the binding is the grant — no visibility or catalog
    intersection), every eligible plugin is deferred (sub-agent runs are
    short-lived and isolated, so there is no sticky activation state to
    consult), and ``skill_filter`` lets the caller apply the same ownership /
    release-exposure narrowing the eager path would have applied to the
    expanded skill ids. Plugins with stdio-transport MCP components are
    returned via ``directory``-absence: the caller expands them eagerly as
    before (their component ids are simply not in ``deferred_*``).
    """
    from core.db.engine import SessionLocal
    from core.db.models import InstalledPlugin

    res = plugin_runtime_models.ProgressiveResolution()
    ids = [i for i in install_ids if isinstance(i, str) and i.strip()]
    if not ids:
        return res

    server_cfgs = _server_configs()
    skill_meta = _skill_metadata()

    try:
        with SessionLocal() as db:
            rows = db.query(InstalledPlugin).filter(InstalledPlugin.install_id.in_(ids)).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] bound plugin load failed: %s", exc)
        return res

    eligible: List[plugin_runtime_models.DeferredPlugin] = []
    for r in rows:
        cids = r.component_ids or {}
        skills = [s for s in (cids.get("skills") or []) if isinstance(s, str) and s.strip()]
        if skill_filter is not None:
            try:
                skills = list(skill_filter(skills))
            except Exception:  # noqa: BLE001
                pass
        mcps = [m for m in (cids.get("mcp") or []) if isinstance(m, str) and m.strip()]
        if not skills and not mcps:
            continue
        if any(not _http_transport(server_cfgs.get(m)) for m in mcps):
            continue
        plugin = plugin_runtime_models.DeferredPlugin(
            install_id=str(r.install_id),
            slug=str(r.slug),
            name=str(r.name or r.slug),
            description=str(r.description or ""),
            skill_ids=skills,
            mcp_ids=mcps,
            bound_mcp_ids=_bound_mcp_ids(skills, skill_meta, mcps),
        )
        eligible.append(plugin)
        res.deferred.append(plugin)

    return _finalize_resolution(res, eligible)


def build_plugin_directory_section(
    directory: Sequence[plugin_runtime_models.DeferredPlugin],
) -> str:
    """Render the stable plugin directory prompt section.

    Deliberately independent of activation state: the same user sees the same
    bytes in every chat and on every turn, so activating a plugin does not
    perturb this part of the prefix.
    """
    if not directory:
        return ""
    lines = [
        "## 插件目录（Progressive Plugins）",
        "以下插件为按需加载：**未加载时其技能与工具不在当前列表中**。"
        "当用户请求匹配某插件的描述、且所需能力不在当前工具/技能列表里时，先调用 "
        "`load_plugin` 工具（参数为插件标识）加载它，加载后的下一步即可使用其"
        "工具与技能。已加载过的插件无需重复调用。",
        "",
        "可用插件（`插件标识`：适用场景）：",
    ]
    for p in directory:
        desc = " ".join((p.description or "").split()) or p.name
        label = f"`{p.slug}`" if p.name == p.slug else f"`{p.slug}`（{p.name}）"
        lines.append(f"- {label}：{desc}")
    return "\n".join(lines)
