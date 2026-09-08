"""Plugin kind on the desktop store: ``R/plugins/<profile>/<slug>/<revision>/plugin.json``.

A plugin is never invoked. Its components are sorted into their own kinds
(skills → the skill store, connectors → mcp bindings) and the plugin directory
keeps only the manifest and its own assets. Readiness is derived: every
required component must itself be ready on this device.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from . import registry, store
from .paths import (
    KIND_AGENT,
    KIND_MCP,
    KIND_PLUGIN,
    KIND_SKILL,
    LOCAL_PROFILE,
    revision_for_hash,
    safe_segment,
)
from .ref import local_ref
from .resolver import SOURCE_LOCAL

logger = logging.getLogger(__name__)


def plugin_manifest_files(definition: Dict[str, Any]) -> Dict[str, str]:
    return {"plugin.json": json.dumps(definition, ensure_ascii=False, sort_keys=True, indent=2)}


def load_manifest(comp: store.StoredComponent) -> Dict[str, Any]:
    return json.loads((comp.path / "plugin.json").read_text(encoding="utf-8"))


def cloud_binding_ids(
    plugin_ids: Iterable[str], *, user_id: Optional[str]
) -> tuple[list[str], list[str]]:
    """Expand selected, authorized cloud definitions without preparing their files."""
    from . import skills
    from .dependency import Context, Inspector, _identifier, component_hash
    from .errors import IntegrityFailed, NameConflict

    if not skills.account_authorized_for(user_id):
        return [], []
    profile = skills.current_account_profile()
    account_profile = profile
    installed = registry.list_installations(kind=KIND_PLUGIN, profile_id=profile)
    selected_skills, selected_mcp = set(), set()

    class BindingCollector(Inspector):
        def visit(self, entry, profile, chain=(), required=True):
            required = bool(required and entry.get("required", True))
            kind, key = str(entry.get("kind") or "unknown"), _identifier(entry)
            if required and kind in (KIND_SKILL, KIND_MCP):
                (selected_skills if kind == KIND_SKILL else selected_mcp).add(key)
            if kind in (KIND_PLUGIN, KIND_AGENT):
                _, inst, comp = self.resolve(kind, str(entry.get("profile") or profile), key)
                if (
                    inst
                    and inst.ready
                    and inst.enabled
                    and comp
                    and comp.profile in (LOCAL_PROFILE, account_profile)
                    and inst.payload.get("owner_user_id") in (None, user_id)
                ):
                    expected = inst.payload.get("resolved_content_hash") or inst.content_hash
                    if expected and component_hash(comp) != expected:
                        raise IntegrityFailed(
                            "selected plugin definition changed", ref=inst.install_id
                        )
            # Inspector traverses only prepared, enabled definitions owned by the
            # current user/profile. Missing required files remain preflight errors.
            super().visit(entry, profile, chain, required)

    collector = BindingCollector(Context(user_id=user_id))
    for ident in plugin_ids:
        matches = [
            row
            for row in installed
            if ident in (row.install_id, row.key, row.payload.get("cloud_install_id"))
        ]
        if len(matches) > 1:
            raise NameConflict("choose the selected plugin source", runtime_name=str(ident))
        if matches:
            collector.visit({"kind": KIND_PLUGIN, "id": matches[0].key}, profile)
    return sorted(selected_skills), sorted(selected_mcp)


def component_install_ids(profile: str, definition: Dict[str, Any]) -> Dict[str, bool]:
    """Ownership edges for a stored plugin manifest: {component install_id: required}."""
    comps = definition.get("components") or {}
    edges: Dict[str, bool] = {}
    for field, kind in (
        ("skills", KIND_SKILL),
        ("agents", KIND_AGENT),
        ("mcp", KIND_MCP),
        ("plugins", KIND_PLUGIN),
    ):
        for entry in comps.get(field) or []:
            if isinstance(entry, dict):
                ident = (
                    entry.get("id")
                    or entry.get("key")
                    or entry.get("skill_id")
                    or entry.get("agent_id")
                    or entry.get("server_id")
                )
                required = bool(entry.get("required", True))
            else:
                ident, required = entry, True
            try:
                edges[registry.install_id(kind, profile, safe_segment(str(ident or "")))] = required
            except ValueError:
                # An invalid required dependency must be surfaced by installation
                # instead of silently turning an incomplete plugin into ready.
                if required:
                    raise ValueError(f"invalid required {kind} component: {ident!r}")
    return edges


def publish_local_plugin(
    definition: Dict[str, Any], *, owner_user_id: Optional[str]
) -> store.StoredComponent:
    """Project a device-installed plugin row into the store and record its components."""
    from core.services.desktop_capability_protocol import entity_content_hash

    slug = safe_segment(str(definition["slug"]))
    files = plugin_manifest_files(definition)
    content_hash = entity_content_hash(files)
    revision = revision_for_hash(content_hash)
    ref = local_ref(KIND_PLUGIN, slug)
    inst = registry.upsert(
        profile_id=LOCAL_PROFILE,
        ref=ref,
        display_name=str(definition.get("name") or slug),
        description=str(definition.get("description") or ""),
        version=str(definition.get("version") or ""),
        content_hash=content_hash,
        source=SOURCE_LOCAL,
        payload={
            "owner_user_id": owner_user_id,
            "from_db": True,
            "db_install_id": definition.get("install_id"),
        },
        enabled=True,
    )
    comp = store.get(KIND_PLUGIN, LOCAL_PROFILE, slug, revision)
    if comp is None:
        comp = store.write_from_files(KIND_PLUGIN, LOCAL_PROFILE, slug, revision, files)
    registry.set_components(inst.install_id, component_install_ids(LOCAL_PROFILE, definition))
    if inst.resolved_revision != revision or inst.state != "ready":
        registry.set_state(inst.install_id, "ready", resolved_revision=revision)
    # Keep historical revisions until a reference-aware collector is available.
    return comp


def remove_local_plugin(slug: str) -> bool:
    from .runtime import references

    if references("plugin", LOCAL_PROFILE, slug):
        # Source deletion revokes use; history still needs its exact bytes.
        return registry.mark_removed(registry.install_id("plugin", LOCAL_PROFILE, slug))
    removed = store.remove_key(KIND_PLUGIN, LOCAL_PROFILE, slug) > 0
    return registry.delete(registry.install_id(KIND_PLUGIN, LOCAL_PROFILE, slug)) or removed


def readiness(
    inst: registry.Installation,
    *,
    cloud_server_ids: Iterable[str],
    managed_enabled: Optional[Dict[str, bool]] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Component-by-component readiness of one plugin installation on this device."""
    cloud_ids = {sid for sid in cloud_server_ids if (managed_enabled or {}).get(sid, True)}
    missing: List[str] = []
    components: List[Dict[str, Any]] = []
    for cid, required in registry.components_of(inst.install_id).items():
        kind, profile, key = cid.split(":", 2)
        if kind in (KIND_SKILL, KIND_AGENT, KIND_PLUGIN):
            comp_inst = registry.get(cid)
            comp = (
                store.get(kind, profile, key, comp_inst.resolved_revision)
                if comp_inst and comp_inst.ready
                else None
            )
            ok = bool(comp_inst and comp_inst.ready and comp_inst.enabled and comp)
            state = (
                ("disabled" if comp_inst and not comp_inst.enabled else comp_inst.state)
                if comp_inst
                else "absent"
            )
        else:  # connector binding
            if profile == LOCAL_PROFILE:
                from core.services.mcp_service import McpServerConfigService

                ok = key in McpServerConfigService.get_instance().get_all_servers(enabled_only=True)
            else:
                ok = key in cloud_ids
            state = "ready" if ok else "absent"
        components.append(
            {
                "install_id": cid,
                "kind": kind,
                "key": key,
                "required": required,
                "ready": ok,
                "state": state,
            }
        )
        if required and not ok:
            missing.append(cid)
    from .dependency import check_installation

    try:
        from core.services.mcp_service import McpServerConfigService

        local_ids = set(McpServerConfigService.get_instance().get_all_servers(enabled_only=True))
    except Exception:
        local_ids = set()
    report = check_installation(
        inst,
        user_id=user_id or inst.payload.get("owner_user_id"),
        available_mcp=local_ids | cloud_ids,
    )
    for error in report["errors"]:
        target = error["dependency_chain"][-1]
        if target not in missing:
            missing.append(target)
    return {
        "ready": inst.ready and inst.enabled and not missing and report["ready"],
        "missing_required": missing,
        "components": components,
        "dependency_report": report,
    }


def enabled_cloud_skill_intents(user_id):
    """Skill intentions of enabled plugins in the current authorized account."""
    from . import skills
    from .dependency import _identifier

    if not skills.account_authorized_for(user_id):
        return set()
    profile = skills.current_account_profile()
    selected = set()
    for row in registry.list_installations(kind=KIND_PLUGIN, profile_id=profile):
        if not row.enabled:
            continue
        for entry in (row.payload.get("components") or {}).get("skills", []):
            sid = _identifier(entry) if isinstance(entry, dict) else str(entry)
            inst = registry.get(registry.install_id(KIND_SKILL, profile, sid))
            if inst and inst.enabled and inst.state != "removed":
                selected.add(sid)
    return selected
