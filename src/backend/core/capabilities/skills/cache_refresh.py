"""Shared cache invalidation after admin skill mutations.

Why centralize: skill changes ripple through 4 caches (loader metadata,
catalog JSON, per-user capability cache, system prompt). Keeping them in
one place prevents drift between admin_skills and admin_skill_drafts routes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def refresh_skill_caches() -> None:
    from core.capabilities.skills.loader_factory import get_skill_loader
    from core.capabilities.catalog.loader import invalidate_catalog_cache
    from core.harness.registry.cache_invalidation import cache_invalidators
    cache_invalidators().run("before_skill_refresh")
    get_skill_loader(reset=True)
    invalidate_catalog_cache()

    try:
        from core.capabilities.catalog.resolver import invalidate_capability_cache

        invalidate_capability_cache()
    except Exception as fallback_error:
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.capabilities.skills.cache_refresh:refresh_skill_caches', fallback_error)

    try:
        from core.capabilities.catalog.runtime import invalidate_runtime_catalog_cache

        invalidate_runtime_catalog_cache()
    except Exception as fallback_error:
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.capabilities.skills.cache_refresh:refresh_skill_caches', fallback_error)

    try:
        cache_invalidators().run("after_skill_refresh")
    except Exception as fallback_error:
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.capabilities.skills.cache_refresh:refresh_skill_caches', fallback_error)

    try:
        from core.capabilities.catalog.loader import load_catalog

        load_catalog(include_runtime_details=False)
    except Exception as exc:
        logger.warning("Failed to eagerly refresh catalog cache after skill mutation: %s", exc)
