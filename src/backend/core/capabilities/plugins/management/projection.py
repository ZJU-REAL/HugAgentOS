"""Project plugin changes into the desktop store and refresh dependent caches."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.capabilities.skills.cache_refresh import refresh_skill_caches
from core.capabilities.catalog.resolver import invalidate_capability_cache

logger = logging.getLogger(__name__)


def _project_plugin_to_store(definition: Dict[str, Any], *, owner_user_id: Optional[str]) -> None:
    """Desktop store: keep ``R/plugins/local/<slug>/`` and the component edges in step."""
    from core.harness.registry.run_capabilities import has_run_capabilities

    if not has_run_capabilities():
        return
    from core.harness.registry.local_publication import local_publication

    local_publication().publish_local_plugin(definition, owner_user_id=owner_user_id)


def _remove_plugin_from_store(slug: str) -> None:
    from core.harness.registry.run_capabilities import has_run_capabilities

    if not has_run_capabilities():
        return
    from core.harness.registry.local_publication import local_publication

    local_publication().remove_local_plugin(slug)


def _purge_sandbox_skill_files(skill_ids) -> None:
    """Drop the materialized files of skills a plugin no longer owns, so they stop showing up under /workspace/skills/."""
    from core.capabilities.skills.config import purge_skill_sandbox_files

    for sid in skill_ids or []:
        try:
            purge_skill_sandbox_files(sid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("purge sandbox files for skill %s failed: %s", sid, exc)


def refresh_after_change(owner_user_id: Optional[str]) -> None:
    """Invalidate related caches after install/uninstall: skill cache + capability-resolution cache + MCP config cache."""
    try:
        refresh_skill_caches()
    except Exception as exc:  # noqa: BLE001
        logger.debug("refresh_skill_caches failed: %s", exc)
    try:
        invalidate_capability_cache(owner_user_id)
        # The 30s capability cache for owned items is keyed by user_id; a global plugin affects all users → clear everything
        if owner_user_id is None:
            invalidate_capability_cache(None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("invalidate_capability_cache failed: %s", exc)
    try:
        from core.capabilities.mcp.service import McpServerConfigService

        McpServerConfigService.get_instance().invalidate_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug("mcp cache invalidate failed: %s", exc)
