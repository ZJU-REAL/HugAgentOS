"""List the MCP servers, skills, plugins and knowledge spaces an agent may bind."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import logging
logger = logging.getLogger(__name__)

def list_available_resources(self, owner_user_id: Optional[str] = None) -> Dict[str, Any]:
    """Return MCP servers, skills, plugins, and KB spaces bindable to agents.

    Plugin-sourced skills/MCP are removed from the skills/mcp_servers lists
    and instead bound as a whole via the ``plugins`` list (plugin =
    installable/removable unit, expanded at runtime into its skills+tools).
    owner_user_id is used to include that user's private plugins and MCPs.

    The MCP list intentionally contains capabilities the user has personally
    switched off.  A sub-agent binding is an explicit, narrower capability
    grant and therefore may opt into one of those tools without turning it
    on for the user's main agent.  Deployment-global MCPs disabled by an
    administrator remain unavailable.
    """
    from core.db.models import AdminMcpServer, InstalledPlugin
    from sqlalchemy import or_
    # ── Plugin list + their component id sets (used to strip plugin capabilities from the loose skills/tools) ──
    # Query the ORM directly for component_ids (authoritative): global plugins + current user's private plugins.
    try:
        pq = self.db.query(InstalledPlugin)
        if owner_user_id:
            pq = pq.filter(
                or_(
                    InstalledPlugin.owner_user_id == owner_user_id,
                    InstalledPlugin.owner_user_id.is_(None),
                )
            )
        else:
            pq = pq.filter(InstalledPlugin.owner_user_id.is_(None))
        plugin_rows = pq.order_by(InstalledPlugin.created_at.desc()).all()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to list installed plugins: %s", exc)
        plugin_rows = []

    plugin_skill_ids: set = set()
    plugin_mcp_ids: set = set()
    # The same plugin may exist both as a global version (install_id=slug@global)
    # and as the user's private version (slug@<uid>) — same name → duplicate
    # display (the user sees two "定时任务管理" entries). Dedupe by slug and
    # show only one: prefer the user's private version (its components carry
    # the user's fingerprint and match their sandbox credentials); fall back
    # to the global version when there is no private one.
    # Note: plugin_skill_ids / plugin_mcp_ids still accumulate from **all**
    # rows (including the deduped-away one), so the loose skill/MCP lists
    # can fully exclude both versions' components.
    _plugin_by_slug: Dict[str, Dict[str, Any]] = {}
    for p in plugin_rows:
        cids = p.component_ids or {}
        s_ids = list(cids.get("skills") or [])
        m_ids = list(cids.get("mcp") or [])
        plugin_skill_ids.update(s_ids)
        plugin_mcp_ids.update(m_ids)
        slug = (p.install_id or "").rsplit("@", 1)[0]
        is_owned = bool(owner_user_id) and p.owner_user_id == owner_user_id
        existing = _plugin_by_slug.get(slug)
        if existing is None or (is_owned and not existing["_owned"]):
            _plugin_by_slug[slug] = {
                "id": p.install_id,
                "name": p.name,
                "description": p.description or "",
                "skill_count": len(s_ids),
                "mcp_count": len(m_ids),
                "_owned": is_owned,
            }
    plugin_list: List[Dict[str, Any]] = [
        {k: v for k, v in item.items() if k != "_owned"} for item in _plugin_by_slug.values()
    ]

    # Built-in plugin MCPs can also be present in the static catalog without
    # a source_plugin DB row.  They still belong under the plugin selector,
    # not the loose MCP selector.
    try:
        from core.capabilities.plugins.management import builtin_plugin_component_ids

        _, builtin_plugin_mcp_ids = builtin_plugin_component_ids()
        plugin_mcp_ids.update(builtin_plugin_mcp_ids)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to load built-in plugin MCP ids: %s", exc)

    # Resolve the user's personal on/off layer once so each MCP option can
    # explain whether it is already enabled for the main agent.  This flag
    # is display metadata only; disabled options remain bindable here.
    enabled_mcp_ids: Optional[set[str]] = None
    if owner_user_id:
        try:
            from core.capabilities.catalog.resolver import resolve_all_runtime_enabled

            _skills, _agents, resolved_mcps = resolve_all_runtime_enabled(
                self.db, owner_user_id
            )
            if resolved_mcps is not None:
                enabled_mcp_ids = set(resolved_mcps)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to resolve user MCP enablement: %s", exc)

    # ── MCP tools (exclude plugin-sourced + owner isolation) ──
    # Owner isolation: private entries (owner_user_id non-null) are visible
    # only to their owner. Otherwise private copies produced by other users
    # installing the same plugin (e.g. automation-automation_task-<their
    # fingerprint>) would all leak into this user's bindable list →
    # duplicates of "定时任务" etc. Empty owner = globally shared entry,
    # visible to everyone.
    _mcp_owner_ok = (
        or_(
            AdminMcpServer.owner_user_id.is_(None),
            AdminMcpServer.owner_user_id == owner_user_id,
        )
        if owner_user_id
        else AdminMcpServer.owner_user_id.is_(None)
    )
    mcp_servers = (
        self.db.query(AdminMcpServer)
        .filter(
            _mcp_owner_ok,
            # Authoritative exclusion: any plugin-sourced MCP (source_plugin
            # nonnull) never enters the loose list — it is bound as a whole via
            # the plugins list instead. More robust than checking only
            # plugin_mcp_ids (doesn't depend on the plugin row still
            # existing/being visible), and also blocks orphaned plugin MCP rows.
            AdminMcpServer.source_plugin.is_(None),
        )
        .order_by(AdminMcpServer.sort_order)
        .all()
    )
    mcp_list: List[Dict[str, Any]] = []
    seen_mcp_ids: set = set()
    for server in mcp_servers:
        # A global false is an administrator lock.  A private false is the
        # owner's personal off state and remains eligible for an explicit
        # sub-agent binding.
        if server.owner_user_id is None and not server.is_enabled:
            continue
        if server.server_id in plugin_mcp_ids:
            continue
        mcp_list.append(
            {
                "id": server.server_id,
                "name": server.display_name,
                "description": server.description,
                "enabled": (
                    server.server_id in enabled_mcp_ids
                    if enabled_mcp_ids is not None
                    else bool(server.is_enabled)
                ),
            }
        )
        seen_mcp_ids.add(server.server_id)

    # Some umbrella/built-in MCP entries are catalog-defined and do not
    # necessarily have a same-named AdminMcpServer row.  Include every
    # administrator-enabled catalog item so the selector is complete.
    try:
        from core.capabilities.catalog.runtime import get_runtime_catalog

        runtime_catalog = get_runtime_catalog(self.db, include_runtime_details=False)
        for item in runtime_catalog.get("mcp") or []:
            item_id = str(item.get("id") or "").strip()
            if (
                not item_id
                or item_id in seen_mcp_ids
                or item_id in plugin_mcp_ids
                or not bool(item.get("enabled", True))
            ):
                continue
            mcp_list.append(
                {
                    "id": item_id,
                    "name": item.get("name") or item_id,
                    "description": item.get("description") or item.get("desc") or "",
                    "enabled": (
                        item_id in enabled_mcp_ids
                        if enabled_mcp_ids is not None
                        else bool(item.get("enabled", True))
                    ),
                }
            )
            seen_mcp_ids.add(item_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to load catalog MCP resources: %s", exc)

    # ── Skills: DB-managed + filesystem-discovered (both exclude plugin-sourced + owner isolation) ──
    # ⚠️ Critical: the filesystem loader (load_all_metadata) scans **all**
    # materialized skills on disk — including other users' private skills
    # and plugin skills materialized by other users' plugin installs.
    # Excluding via plugin_skill_ids alone (components of plugins visible to
    # the current user only) would miss some, letting other users' plugin
    # skills sneak into "bindable skills". So compute two **authoritative**
    # exclusion sets directly from AdminSkill and filter both sources
    # uniformly:
    #   - all_plugin_skill_ids: any plugin-sourced skill (source_plugin non-null, any owner);
    #   - foreign_private_skill_ids: other users' private skills (owner_user_id non-null and ≠ current user).
    from core.db.models import AdminSkill

    all_plugin_skill_ids: set = set()
    foreign_private_skill_ids: set = set()
    try:
        for row in self.db.query(
            AdminSkill.skill_id, AdminSkill.source_plugin, AdminSkill.owner_user_id
        ).all():
            if row.source_plugin:
                all_plugin_skill_ids.add(row.skill_id)
            if row.owner_user_id and row.owner_user_id != owner_user_id:
                foreign_private_skill_ids.add(row.skill_id)
    except Exception as exc:
        logger.debug("Failed to precompute skill exclusion sets: %s", exc)

    _excluded_skill_ids = plugin_skill_ids | all_plugin_skill_ids | foreign_private_skill_ids

    skill_list: List[Dict[str, Any]] = []
    try:
        _skill_owner_ok = (
            or_(AdminSkill.owner_user_id.is_(None), AdminSkill.owner_user_id == owner_user_id)
            if owner_user_id
            else AdminSkill.owner_user_id.is_(None)
        )
        db_skills = (
            self.db.query(AdminSkill)
            .filter(
                AdminSkill.is_enabled == True,
                _skill_owner_ok,
            )
            .order_by(AdminSkill.updated_at.desc())
            .all()
        )
        seen_ids = set()
        for s in db_skills:
            if s.skill_id in _excluded_skill_ids:
                continue
            skill_list.append(
                {"id": s.skill_id, "name": s.display_name, "description": s.description or ""}
            )
            seen_ids.add(s.skill_id)
    except Exception:
        seen_ids = set()

    try:
        from core.capabilities.skills.loader_factory import get_skill_loader

        loader = get_skill_loader()
        for sid, meta in loader.load_all_metadata().items():
            if sid not in seen_ids and sid not in _excluded_skill_ids:
                skill_list.append(
                    {
                        "id": sid,
                        "name": getattr(meta, "name", sid),
                        "description": getattr(meta, "description", ""),
                    }
                )
    except Exception as exc:
        logger.debug("Failed to load filesystem skills: %s", exc)

    # KB spaces
    kb_list: List[Dict[str, Any]] = []
    try:
        from core.harness.registry.catalog_options import catalog_options
        kb_list = catalog_options("kb_spaces", self.db)
    except Exception as fallback_error:
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.capabilities.agents.available_resources:list_available_resources', fallback_error)

    return {
        "mcp_servers": mcp_list,
        "skills": skill_list,
        "plugins": plugin_list,
        "kb_spaces": kb_list,
        "ontology_tags": self._ontology_tag_options(),
    }
