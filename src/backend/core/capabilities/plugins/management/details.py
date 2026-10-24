"""Marketplace plugin details and per-skill exclusions."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.harness.registry.marketplace_catalog import marketplace_catalog
from core.infra.exceptions import BadRequestError
from core.capabilities.plugins.management import admin_config as plugin_admin_config
from core.capabilities.plugins.packaging.presentation import overlay_market_meta
from core.capabilities.plugins.packaging.models import NormalizedPlugin, NormalizedSkill
from core.capabilities.skills.installation import strip_frontmatter as _strip_frontmatter
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _normalize_market_plugin(slug: str, db: Optional[Session]) -> NormalizedPlugin:
    from core.harness.registry.package_sources import package_source
    return package_source("marketplace").normalize(db, slug)


def get_market_skill_exclusions(db: Optional[Session], slug: str) -> set:
    """The set of skill names the admin has "removed" from the marketplace for this plugin."""
    return marketplace_catalog().excluded_skills(db, slug)


def get_plugin_detail(slug: str, db: Optional[Session] = None) -> Dict[str, Any]:
    """Marketplace plugin detail (after normalize: component list + required secrets + drop preview).

    Filesystem preset bundle first; falls back to the DB marketplace package
    (admin-uploaded and published) when not found. Skills the admin removed
    from the marketplace (``PluginMarketSkillExclusion``) are not shown in the
    detail.
    """
    np = _normalize_market_plugin(slug, db)
    detail = normalized_to_detail(np, excluded=get_market_skill_exclusions(db, slug))
    overlay_market_meta(detail, marketplace_catalog().plugin_metadata(db, slug))
    return detail


def exclude_market_skill(
    db: Session, slug: str, skill_name: str, *, created_by: Optional[str] = None
) -> Dict[str, Any]:
    """ "Remove" a skill from a marketplace plugin: record one exclusion (idempotent).

    Validates the skill actually belongs to this plugin (guards against
    typos); afterwards the marketplace list/detail/install no longer include
    it. Installed instances are untouched.
    """
    raw_names = {s.name for s in _normalize_market_plugin(slug, db).skills}
    if skill_name not in raw_names:
        raise BadRequestError(message=f"技能 {skill_name!r} 不属于插件 {slug!r}")
    if marketplace_catalog().exclude_skill(db, slug, skill_name, created_by=created_by):
        logger.info("plugin_market_skill_excluded: slug=%s skill=%s", slug, skill_name)
    return {"slug": slug, "skill_name": skill_name, "excluded": True}


def _skill_component_preview(sk: NormalizedSkill) -> Dict[str, Any]:
    """Preview component for a not-yet-installed builtin/external skill: includes body instructions + file list for pre-install inspection."""
    from core.capabilities.skills.registry import split_frontmatter

    desc = ""
    tags: List[str] = []
    try:
        fm, _ = split_frontmatter(sk.skill_content or "")
        desc = (fm.get("description") or "").strip()
        raw_tags = fm.get("tags") or ""
        if isinstance(raw_tags, str) and raw_tags:
            tags = [x.strip() for x in raw_tags.replace("，", ",").split(",") if x.strip()]
    except Exception as fallback_error:  # noqa: BLE001
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.capabilities.plugins.management.details:_skill_component_preview', fallback_error)
    files = sorted(k for k in (sk.extra_files or {}).keys() if k != "secrets.json")
    return {
        "skill_id": sk.name,
        "name": sk.name,
        "description": desc,
        "version": "",
        "tags": tags,
        "enabled": True,  # enabled by default after install (preview semantics)
        "instructions": _strip_frontmatter(sk.skill_content),
        "files": files,
        "has_secrets": False,
    }


def normalized_to_detail(
    np: NormalizedPlugin, *, excluded: Optional[set] = None
) -> Dict[str, Any]:
    excluded = excluded or set()
    return {
        "slug": np.slug,
        "name": np.name,
        "version": np.version,
        "description": np.description,
        "category": np.category,
        "icon": np.icon,
        "kind": np.kind,
        "required_secrets": np.required_secrets,
        "admin_config": plugin_admin_config._admin_config_view(np.admin_config, with_values=False),
        "connection": np.connection,
        "skills": [_skill_component_preview(s) for s in np.skills if s.name not in excluded],
        "mcp": [
            {
                "server_id": m.name,
                "name": m.name,
                "description": m.description,
                "transport": m.transport,
                "url": m.url,
                "enabled": not m.needs_runtime,
                "needs_runtime": m.needs_runtime,
                "note": m.note,
                "tools": list(getattr(m, "tools", None) or []),
            }
            for m in np.mcp
        ],
        "dropped": np.dropped,
    }
