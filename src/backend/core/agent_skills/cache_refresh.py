"""Shared cache invalidation after admin skill mutations.

Why centralize: skill changes ripple through 4 caches (loader metadata,
catalog JSON, per-user capability cache, system prompt). Keeping them in
one place prevents drift between admin_skills and admin_skill_drafts routes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def refresh_skill_caches() -> None:
    from core.agent_skills.loader import get_skill_loader
    from core.config.catalog_loader import invalidate_catalog_cache

    get_skill_loader(reset=True)
    invalidate_catalog_cache()

    # Skill content changed -> invalidate the tar archive cache; the next push repacks with fresh material (cube uses packed transport)
    try:
        from core.agent_skills.skill_archive import clear_cache as _clear_tar_cache

        _clear_tar_cache()
    except Exception:
        pass

    try:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache()
    except Exception:
        pass

    try:
        from core.config.catalog_runtime import invalidate_runtime_catalog_cache

        invalidate_runtime_catalog_cache()
    except Exception:
        pass

    try:
        from prompts.prompt_runtime import invalidate_prompt_cache

        invalidate_prompt_cache()
    except Exception:
        pass

    try:
        from core.config.catalog_loader import load_catalog

        load_catalog(include_runtime_details=False)
    except Exception as exc:
        logger.warning("Failed to eagerly refresh catalog cache after skill mutation: %s", exc)
