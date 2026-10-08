"""Progressive plugin runtime: desktop resolution."""

from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger(__name__)


from core.plugins.runtime import models as plugin_runtime_models
from core.plugins.runtime import resolution as plugin_resolution


def prepare_desktop_plugin_skill_defaults(user_id, skill_ids):
    """Include enabled cloud plugin intentions before ready-only skill filtering."""
    from core.capabilities import skills
    from core.capabilities.plugins import enabled_cloud_skill_intents
    from core.capabilities.preparation import ensure_cloud_ready

    selected = set(skill_ids) | enabled_cloud_skill_intents(user_id)
    ensure_cloud_ready(user_id, skill_keys=sorted(selected))
    return skills.filter_available_names(sorted(selected), user_id=user_id)


def resolve_desktop_progressive_plugins(
    *,
    user_id,
    enabled_skill_ids,
    enabled_mcp_ids,
    plugin_ids=None,
    activated_ids=(),
    invoked_skill_ids=(),
    invoked_mcp_ids=(),
):
    """Defer an authorized device plugin without changing the run's source selection.

    Preparation still freezes the complete selected closure before execution.
    Only model-facing skills and MCP connections are delayed until load_plugin.
    The device registry, not legacy cloud InstalledPlugin rows, owns identities.
    """
    from core.capabilities import registry, skills
    from core.capabilities.dependency import Context, Inspector, _identifier
    from core.capabilities.paths import LOCAL_PROFILE

    profile = skills.current_account_profile() if skills.account_authorized_for(user_id) else None
    result = plugin_runtime_models.ProgressiveResolution()
    eligible: List[plugin_runtime_models.DeferredPlugin] = []
    allowed_skills, allowed_mcp = set(enabled_skill_ids or []), set(enabled_mcp_ids or [])
    active = {item for item in (activated_ids or []) if item}
    from core.capabilities.errors import CapabilityError
    from core.capabilities.preparation import ensure_cloud_ready

    for skill in sorted(allowed_skills):
        try:
            ensure_cloud_ready(user_id, skill_keys=[skill])
        except (CapabilityError, OSError, ValueError):
            result.unavailable_skill_ids.add(skill)
    choices = skills.resolve_for_user(user_id)
    bindings = {
        name: {"install_id": candidate.install_id, "revision": candidate.revision}
        for name, candidate in choices.chosen.items()
    }
    installations = {
        row.install_id: row for row in registry.list_installations(include_removed=True)
    }
    # Cloud projections and explicitly owned local packages are both valid.
    # Legacy shared local bundles must stay hidden in hybrid mode: their MCP
    # endpoints belong to a different execution plane.
    from core.capabilities import device_catalog

    hybrid = device_catalog.active() and profile
    allowed_profiles = (LOCAL_PROFILE, profile)
    prepared_rows = []
    for row in list(installations.values()):
        if row.kind != "plugin" or row.state == "removed":
            continue
        if row.profile_id not in allowed_profiles or not row.enabled:
            continue
        if row.payload.get("owner_user_id") not in (None, "", user_id):
            continue
        if hybrid and row.profile_id == LOCAL_PROFILE:
            if row.payload.get("owner_user_id") != user_id:
                continue
        from core.capabilities.local_plugin_runtime import enabled_for

        if not enabled_for(row, user_id):
            continue
        aliases = {
            row.install_id,
            row.key,
            row.payload.get("cloud_install_id"),
            row.payload.get("db_install_id"),
        }
        if plugin_ids is not None and not aliases.intersection(plugin_ids):
            continue
        if not row.ready:
            # A fresh manifest contains intentions, not definition files. Prepare
            # only a definition whose components are selected in this run.
            advertised = row.payload.get("components") or {}
            advertised_skills = {
                _identifier(x) if isinstance(x, dict) else str(x)
                for x in advertised.get("skills", [])
            }
            advertised_mcp = {
                _identifier(x) if isinstance(x, dict) else str(x) for x in advertised.get("mcp", [])
            }
            if plugin_ids is None and not (
                advertised_skills & allowed_skills or advertised_mcp & allowed_mcp
            ):
                continue
            from core.services import desktop_cloud_bridge, desktop_cloud_bundles

            results = desktop_cloud_bundles.prepare(
                desktop_cloud_bridge.get_state(), [row.install_id]
            )
            if not results or not results[0]["ok"]:
                result.unavailable_skill_ids.update(advertised_skills & allowed_skills)
                continue
            installations = {
                item.install_id: item for item in registry.list_installations(include_removed=True)
            }
            row = installations.get(row.install_id)
        prepared_rows.append((row, aliases))

    def inspect_plugin(prepared):
        row, aliases = prepared
        mcp_ids, declared_skills = set(), set()

        def record_component(entry, required):
            if entry.get("kind") == "skill":
                declared_skills.add(_identifier(entry))
            if entry.get("kind") == "mcp" and required:
                mcp_ids.add(_identifier(entry))
            return True

        inspector = Inspector(
            Context(
                user_id=user_id,
                available_mcp=allowed_mcp,
                bindings=bindings,
                installations=installations,
            ),
            on_visit=record_component,
        )
        inspector.visit({"kind": "plugin", "id": row.install_id}, row.profile_id)
        report = inspector.report()
        from core.capabilities.errors import IntegrityFailed, PackageMissing

        nodes = [node for node in report["nodes"] if node["kind"] in ("plugin", "agent")]
        for error in report["errors"]:
            unselected = any(
                (part.split(":", 1)[0] == "skill" and part.split(":")[-1] not in allowed_skills)
                or (part.split(":", 1)[0] == "mcp" and part.split(":")[-1] not in allowed_mcp)
                for part in error["dependency_chain"]
            )
            if not unselected:
                raise PackageMissing(
                    "plugin definition dependencies are not ready",
                    ref=row.install_id,
                    details={"dependency": error},
                )
        for node in nodes:
            inst = installations.get(node["install_id"])
            expected = inst.payload.get("resolved_content_hash") or inst.content_hash
            if expected and expected != node["content_hash"]:
                raise IntegrityFailed("plugin definition changed", ref=node["install_id"])
        skill_ids = sorted(declared_skills & allowed_skills)
        unavailable_mcp_ids = mcp_ids - allowed_mcp
        mcp_ids &= allowed_mcp
        if not skill_ids and not mcp_ids and not unavailable_mcp_ids:
            return None
        item = plugin_runtime_models.DeferredPlugin(
            row.install_id,
            row.key,
            row.display_name or row.key,
            row.description or "",
            skill_ids,
            sorted(mcp_ids),
            capability_nodes=nodes,
            unavailable_mcp_ids=unavailable_mcp_ids,
        )
        return item, aliases, skill_ids, mcp_ids

    def inspect_plugin_or_skip(prepared):
        """一个装不起来的插件只是这一轮不出现，不牵连别的插件。"""
        try:
            return inspect_plugin(prepared)
        except (CapabilityError, OSError, ValueError):
            return None

    # Downloads above are serial. Workers share only the completed detached
    # registry snapshot, and each owns its walker and component-name sets.
    if os.name == "nt" and len(prepared_rows) >= 8:
        from concurrent.futures import ThreadPoolExecutor
        from contextvars import copy_context

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cap-plugins") as pool:
            pending = [
                pool.submit(copy_context().run, inspect_plugin_or_skip, row)
                for row in prepared_rows
            ]
            inspected = [future.result() for future in pending]
    else:
        inspected = map(inspect_plugin_or_skip, prepared_rows)
    for resolved in inspected:
        if resolved is None:
            continue
        item, aliases, skill_ids, mcp_ids = resolved
        if item.unavailable_mcp_ids:
            # An installed instruction is not a working plugin. Never advertise
            # a skill-only activation when its required tool binding is absent,
            # nor substitute another account's similarly named connector.
            result.unavailable_skill_ids.update(skill_ids)
            logger.info(
                "[plugin-loader] plugin %s unavailable: missing MCP %s",
                item.install_id,
                ", ".join(sorted(item.unavailable_mcp_ids)),
            )
            continue
        # Explicit source-qualified selectors avoid silently choosing a namesake.
        if any(p.slug == item.slug for p in eligible):
            for previous in eligible:
                if previous.slug == item.slug:
                    previous.slug = previous.install_id
            item.slug = item.install_id
        eligible.append(item)
        if (
            aliases.intersection(active)
            or set(skill_ids).intersection(invoked_skill_ids or ())
            or mcp_ids.intersection(invoked_mcp_ids or ())
        ):
            result.activated_slugs.append(item.slug)
        else:
            result.deferred.append(item)
    # A shared skill may still belong to another complete, authorized plugin.
    result.unavailable_skill_ids.difference_update(
        sid for item in eligible for sid in item.skill_ids
    )
    unavailable_invoked = result.unavailable_skill_ids.intersection(invoked_skill_ids or ())
    if unavailable_invoked:
        from core.capabilities.errors import PackageMissing

        raise PackageMissing(
            "selected skill belongs to a plugin with unavailable MCP servers",
            runtime_name=", ".join(sorted(unavailable_invoked)),
        )
    return plugin_resolution._finalize_resolution(result, eligible)
