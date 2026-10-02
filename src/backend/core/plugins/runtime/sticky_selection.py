"""Progressive plugin runtime: sticky selection."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


from core.plugins.runtime import activation_history as plugin_activation_history
from core.plugins.runtime import models as plugin_runtime_models


def _cloud_sticky_selection(tokens, *, user_id, allow_aliases=False, unavailable_out=None):
    """Keep saved cloud selections tied to their exact account and installation."""
    from core.capabilities import registry, skills
    from core.capabilities.errors import NameConflict, PermissionDenied
    from core.capabilities.paths import BUILTIN_PROFILE, LOCAL_PROFILE, capabilities_enabled

    if not capabilities_enabled():
        return []
    profile = skills.current_account_profile()
    authorized = skills.account_authorized_for(user_id)
    rows = (
        registry.list_installations(kind="plugin", profile_id=profile)
        if profile and authorized
        else []
    )
    result = []
    for token in tokens:
        parts = str(token).split(":", 2)
        scoped = (
            len(parts) == 3
            and parts[0] == "plugin"
            and parts[1] not in (LOCAL_PROFILE, BUILTIN_PROFILE)
        )
        if scoped and (not authorized or parts[1] != profile):
            raise PermissionDenied("saved plugin belongs to another cloud account")
        matches = [
            row
            for row in rows
            if token == row.install_id
            or (allow_aliases and token in (row.key, row.payload.get("cloud_install_id")))
        ]
        if len(matches) > 1:
            raise NameConflict("choose the saved plugin source")
        if not matches:
            if scoped:
                if unavailable_out is not None:
                    unavailable_out.append(token)
                    continue
                raise PermissionDenied("saved plugin is no longer authorized")
            continue
        row = matches[0]
        if (
            not row.ready
            or not row.enabled
            or row.payload.get("owner_user_id") not in (None, user_id)
        ):
            if unavailable_out is not None and row.payload.get("owner_user_id") in (None, user_id):
                unavailable_out.append(token)
                continue
            raise PermissionDenied("saved plugin is disabled or unavailable")
        result.append(row)
    return result


def resolve_sticky_plugin_capabilities(
    *,
    user_id: str,
    chat_id: Optional[str],
) -> plugin_runtime_models.StickyPluginCapabilities:
    """Resolve durable plugin activations before normal capability narrowing.

    Personal catalog switches intentionally do not participate: once the user
    explicitly loads a plugin in a chat, its components remain expanded for
    that chat. Every turn still revalidates the installation's visibility and
    each component's admin/global state, dependency readiness and ownership;
    uninstalling or administratively disabling a component therefore removes
    it immediately.
    """
    tokens = plugin_activation_history.load_activated_plugin_slugs(chat_id)
    result = plugin_runtime_models.StickyPluginCapabilities()
    if not tokens or not user_id:
        return result

    # Legacy unscoped tokens remain local-only. New cloud activations are saved
    # with their canonical profile so an account switch cannot retarget a slug.
    cloud = _cloud_sticky_selection(
        tokens,
        user_id=user_id,
        unavailable_out=result.unavailable_ids,
    )
    if cloud:
        from core.capabilities.errors import CapabilityError
        from core.capabilities.plugins import cloud_binding_ids

        for row in cloud:
            try:
                skill_ids, mcp_ids = cloud_binding_ids([row.install_id], user_id=user_id)
            except (CapabilityError, OSError, ValueError):
                result.unavailable_ids.append(row.install_id)
                continue
            result.install_ids.append(row.install_id)
            result.slugs.append(row.key)
            result.skill_ids.extend(skill_ids)
            result.mcp_ids.extend(mcp_ids)

    try:
        from core.config.catalog_resolver import resolve_explicit_runtime_capabilities
        from core.db.engine import SessionLocal
        from core.db.models import InstalledPlugin
        from sqlalchemy import or_

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
            by_id = {str(row.install_id): row for row in rows}
            by_slug: Dict[str, List[Any]] = {}
            for row in rows:
                by_slug.setdefault(str(row.slug), []).append(row)

            selected: List[Any] = []
            seen_install_ids: Set[str] = set()
            for token in tokens:
                row = by_id.get(token)
                if row is None:
                    # Legacy activation rows stored only the slug. Prefer the
                    # user's own installation when a private/global duplicate
                    # exists, matching catalog visibility semantics.
                    matches = by_slug.get(token) or []
                    matches = sorted(
                        matches,
                        key=lambda item: (item.owner_user_id != user_id, str(item.install_id)),
                    )
                    row = matches[0] if matches else None
                install_id = str(getattr(row, "install_id", "") or "")
                if row is not None and install_id and install_id not in seen_install_ids:
                    selected.append(row)
                    seen_install_ids.add(install_id)

            requested_skills: List[str] = []
            requested_mcps: List[str] = []
            from core.plugins.packaging.sources import _component_keys

            for row in selected:
                component_ids = row.component_ids or {}
                requested_skills.extend(_component_keys(component_ids, "skills"))
                requested_mcps.extend(_component_keys(component_ids, "mcp"))
            requested_skills = list(dict.fromkeys(requested_skills))
            requested_mcps = list(dict.fromkeys(requested_mcps))
            allowed_skills, allowed_mcps, unavailable_skills, unavailable_mcps = (
                resolve_explicit_runtime_capabilities(
                    db,
                    user_id,
                    skill_ids=requested_skills,
                    mcp_ids=requested_mcps,
                )
            )

            result.install_ids = list(
                dict.fromkeys([*result.install_ids, *[str(row.install_id) for row in selected]])
            )
            result.slugs = list(
                dict.fromkeys([*result.slugs, *[str(row.slug) for row in selected]])
            )
            result.skill_ids = list(dict.fromkeys([*result.skill_ids, *allowed_skills]))
            result.mcp_ids = list(dict.fromkeys([*result.mcp_ids, *allowed_mcps]))
            if unavailable_skills or unavailable_mcps:
                logger.info(
                    "[plugin-loader] sticky activation partially unavailable "
                    "chat=%s installs=%s skipped_skills=%s skipped_mcps=%s",
                    chat_id,
                    result.install_ids,
                    unavailable_skills,
                    unavailable_mcps,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] sticky capability resolve failed: %s", exc)
    return result
