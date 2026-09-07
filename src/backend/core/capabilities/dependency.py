"""Read-only dependency closure for capability preparation and runtime preflight.

Supported declarations: components.{skills,agents,mcp,plugins}, dependencies as
[{kind,id,required,version_constraint,platforms}], and agent's explicit ID lists.
Runtime package maps support pip; npm/apt without a verified runtime resolver are
reported as unverified rather than installed or assumed available.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from . import registry, skills, store
from .errors import CapabilityError
from .paths import BUILTIN_PROFILE, LOCAL_PROFILE


class DependencyMissing(CapabilityError):
    code = "dependency_missing"
    recovery_action = "inspect_dependencies"


_FIELDS = {"skills": "skill", "agents": "agent", "mcp": "mcp", "plugins": "plugin"}
_AGENT_FIELDS = {
    "skill_ids": "skill",
    "mcp_server_ids": "mcp",
    "plugin_ids": "plugin",
    "kb_ids": "kb",
}


def _entries(definition, kind, depth=0):
    if depth > 32 or not isinstance(definition, dict):
        return [{"kind": "unknown", "id": "invalid-dependency-declaration"}]
    entries = []
    components = definition.get("components")
    if isinstance(components, dict):
        for group, values in components.items():
            for value in values if isinstance(values, list) else [values]:
                entry = dict(value) if isinstance(value, dict) else {"id": value}
                entry.setdefault("kind", _FIELDS.get(group, group))
                entries.append(entry)
    for group, dep_kind in _AGENT_FIELDS.items():
        values = definition.get(group) or []
        if isinstance(values, str):
            values = values.replace(",", " ").split()
        for value in values:
            entry = dict(value) if isinstance(value, dict) else {"id": value}
            entry.setdefault("kind", dep_kind)
            entries.append(entry)
    mcp_names = definition.get("mcp_servers") or definition.get("mcp-server-ids")
    if mcp_names:
        for name in (
            mcp_names if isinstance(mcp_names, list) else str(mcp_names).replace(",", " ").split()
        ):
            entries.append({"kind": "mcp", "id": name})
    if definition.get("model_provider_id"):
        entries.append({"kind": "model", "id": definition["model_provider_id"]})
    dependencies = definition.get("dependencies") or []
    if isinstance(dependencies, list):
        entries.extend(
            dict(value) if isinstance(value, dict) else {"kind": "unknown", "id": value}
            for value in dependencies
        )
    elif isinstance(dependencies, dict):
        for dep_kind, values in dependencies.items():
            if dep_kind == "warnings":
                continue
            for value in values if isinstance(values, list) else [values]:
                entry = dict(value) if isinstance(value, dict) else {"id": value}
                entry.setdefault("kind", dep_kind)
                entries.append(entry)
    else:
        entries.append({"kind": "unknown", "id": "invalid-dependencies"})
    extra = definition.get("extra_config") or {}
    if isinstance(extra, dict) and extra.get("capability_requirements"):
        requirements = extra["capability_requirements"]
        if isinstance(requirements, list):
            requirements = {"dependencies": requirements}
        entries += _entries(requirements, kind, depth + 1)
    raw_extensions = definition.get("extensions") or []
    if isinstance(raw_extensions, dict):
        extensions = []
        for key, value in raw_extensions.items():
            if key in (
                "architecture",
                "python_version",
                "node_version",
                "execution_plane",
                "platforms",
            ):
                continue  # Inspected by platform_ok below, never assumed satisfied.
            item = dict(value) if isinstance(value, dict) else {"id": key}
            item.setdefault("id", key)
            extensions.append(item)
    else:
        extensions = list(raw_extensions)
    for key in ("hooks", "rules", "commands"):
        values = definition.get(key)
        if values:
            for value in values if isinstance(values, list) else [values]:
                item = dict(value) if isinstance(value, dict) else {"id": key}
                extensions.append(item)
    for extension in extensions:
        value = dict(extension) if isinstance(extension, dict) else {"id": extension}
        entries.append({**value, "kind": "unsupported_extension"})
    return entries


def _identifier(entry):
    return str(
        entry.get("id")
        or entry.get("key")
        or entry.get("skill_id")
        or entry.get("agent_id")
        or entry.get("server_id")
        or ""
    )


def skill_definition(path: Path):
    import yaml

    text = (path / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    pieces = text.split("---", 2)
    metadata = yaml.safe_load(pieces[1]) if len(pieces) > 2 else {}
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("skill frontmatter must be an object")
    return metadata


def component_hash(comp):
    if comp.kind == "skill":
        return skills.skill_dir_hash(comp.path, fresh=True)
    from core.services.desktop_capability_protocol import entity_content_hash

    from .archive import iter_files

    return entity_content_hash(
        {
            rel: path.read_text(encoding="utf-8")
            for rel, path in iter_files(comp.path)
            if rel != ".inventory.json"
        }
    )


@dataclass
class Context:
    user_id: Optional[str] = None
    bindings: Optional[dict] = None
    available_mcp: Optional[set] = None
    available_kb: Optional[set] = None
    available_models: Optional[set] = None
    platform_name: str = field(default_factory=lambda: platform.system().lower())
    runtime_versions: Optional[dict] = None
    frozen_nodes: dict = field(default_factory=dict)


class Inspector:
    def __init__(self, context, on_visit=None):
        self.context = context
        self.errors, self.warnings, self.nodes = [], [], {}
        # Called as ``on_visit(entry, required)`` before each entry is
        # inspected; returning False skips it and its subtree. Lets a caller
        # observe or narrow one traversal without subclassing the walker.
        self._on_visit = on_visit

    def issue(self, reason, chain, required=True, **details):
        item = {
            "code": "dependency_missing",
            "reason": reason,
            "dependency_chain": list(chain),
            **details,
        }
        (self.errors if required else self.warnings).append(item)

    def platform_ok(self, definition, chain, required):
        extension_constraints = definition.get("extensions")
        if isinstance(extension_constraints, dict):
            definition = {**extension_constraints, **definition}
        if definition.get("execution_plane") not in (None, "local"):
            self.issue(
                "execution_plane_incompatible",
                chain,
                required,
                recovery_action="switch_execution_plane",
            )
        for constraint in ("architecture", "python_version", "node_version"):
            if definition.get(constraint):
                self.issue("runtime_constraint_unverified", chain, required, constraint=constraint)
        value = definition.get("platforms") or definition.get("platform")
        if not value:
            return
        names = [value] if isinstance(value, str) else value
        aliases = {"win32": "windows", "macos": "darwin", "mac": "darwin"}
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            self.issue("platform_declaration_invalid", chain, required)
        elif self.context.platform_name not in [
            aliases.get(name.lower(), name.lower()) for name in names
        ]:
            self.issue(
                "platform_incompatible",
                chain,
                required,
                current_platform=self.context.platform_name,
                declared_platforms=names,
            )

    def version_ok(self, entry, version, chain, required):
        constraint = str(entry.get("version_constraint") or entry.get("version") or "")
        if not constraint:
            return
        if not version:
            self.issue("version_unknown", chain, required, version_constraint=constraint)
            return
        try:
            spec = SpecifierSet(constraint if constraint[0] in "<>=!~" else "==" + constraint)
            if Version(str(version).lstrip("vV")) not in spec:
                self.issue(
                    "version_mismatch",
                    chain,
                    required,
                    installed_version=version,
                    version_constraint=constraint,
                )
        except Exception:
            self.issue(
                "version_constraint_unsupported", chain, required, version_constraint=constraint
            )

    def external(self, entry, chain, required):
        kind, key = entry.get("kind"), _identifier(entry)
        self.platform_ok(entry, chain, required)
        if kind in ("mcp", "kb", "model"):
            available = getattr(
                self.context, "available_" + {"mcp": "mcp", "kb": "kb", "model": "models"}[kind]
            )
            if available is None:
                self.issue("authorization_unverified", chain, required)
            elif key not in available:
                self.issue("not_authorized_or_missing", chain, required)
            self.version_ok(entry, None, chain, required)
            return
        if kind in ("pip", "npm", "apt"):
            version = (self.context.runtime_versions or {}).get(kind + ":" + key)
            if kind == "pip" and self.context.runtime_versions is None:
                try:
                    requirement = Requirement(key)
                    if requirement.marker and not requirement.marker.evaluate():
                        return
                    version = importlib.metadata.version(requirement.name)
                    if requirement.specifier and Version(version) not in requirement.specifier:
                        self.issue("runtime_version_mismatch", chain, required)
                        return
                except importlib.metadata.PackageNotFoundError:
                    self.issue("runtime_dependency_missing", chain, required)
                    return
                except Exception:
                    self.issue("runtime_requirement_invalid", chain, required)
                    return
            if not version:
                self.issue(
                    "runtime_dependency_unverified",
                    chain,
                    required,
                    recovery_action="verify_runtime_dependency",
                )
            else:
                self.version_ok(entry, version, chain, required)
            return
        self.issue("dependency_kind_unsupported", chain, required)

    def resolve(self, kind, profile, key):
        if ":" in key:
            parts = key.split(":", 2)
            if len(parts) != 3 or parts[0] != kind:
                return None, None, None
            _, selected_profile, key = parts
            if selected_profile not in (LOCAL_PROFILE, BUILTIN_PROFILE):
                if (
                    selected_profile != skills.current_account_profile()
                    or not skills.account_authorized_for(self.context.user_id)
                ):
                    return None, None, None
            profile = selected_profile
        if kind == "skill" and self.context.bindings is not None:
            binding = self.context.bindings.get(key)
            if binding is None:
                return None, None, None
            iid = binding["install_id"]
            bound_kind, profile, stored_key = iid.split(":", 2)
            if bound_kind != kind:
                return None, None, None
            comp = store.get(kind, profile, stored_key, binding["revision"])
            return iid, registry.get(iid), comp
        iid = registry.install_id(kind, profile, key)
        inst = registry.get(iid)
        if inst is None and kind == "plugin":
            inst = next(
                (
                    row
                    for row in registry.list_installations(
                        kind="plugin", profiles=[LOCAL_PROFILE, profile]
                    )
                    if key
                    in (row.payload.get("db_install_id"), row.payload.get("cloud_install_id"))
                ),
                None,
            )
            if inst:
                iid, profile, key = inst.install_id, inst.profile_id, inst.key
        frozen = self.context.frozen_nodes.get(iid) or {}
        revision = frozen.get("revision") or (inst.resolved_revision if inst else None)
        comp = store.get(kind, profile, key, revision) if inst and inst.ready else None
        return iid, inst, comp

    def effective_version(self, kind, key, label, inst):
        """The version a requirement is checked against.

        A frozen node wins, then the caller's skill binding (the run may have
        chosen a source the installation row does not describe), then whatever
        the installation itself reports.
        """
        pinned = self.context.frozen_nodes.get(label) or {}
        if "version" in pinned:
            return pinned["version"]
        if kind == "skill":
            bound = (self.context.bindings or {}).get(key.split(":")[-1], {})
            if "version" in bound:
                return bound["version"]
        return inst.version if inst else None

    def visit(self, entry, profile, chain=(), required=True):
        required = bool(required and entry.get("required", True))
        if self._on_visit is not None and not self._on_visit(entry, required):
            return
        kind, key = str(entry.get("kind") or "unknown"), _identifier(entry)
        if kind not in ("skill", "agent", "plugin"):
            self.external(entry, [*chain, kind + ":" + key], required)
            return
        try:
            iid, inst, comp = self.resolve(kind, str(entry.get("profile") or profile), key)
        except (ValueError, TypeError):
            iid, inst, comp = None, None, None
        label = iid or kind + ":" + key
        path = [*chain, label]
        if label in chain or len(chain) >= 64:
            self.issue("dependency_cycle", path, required)
            return
        if comp is None or (inst is not None and not (inst.ready and inst.enabled)):
            self.issue("component_not_ready", path, required)
            return
        if comp.profile not in (LOCAL_PROFILE, BUILTIN_PROFILE, skills.current_account_profile()):
            self.issue("not_authorized_or_missing", path, required)
            return
        if (
            inst
            and inst.payload.get("owner_user_id")
            and self.context.user_id != inst.payload["owner_user_id"]
        ):
            self.issue("not_authorized_or_missing", path, required)
            return
        self.platform_ok(entry, path, required)
        version = self.effective_version(kind, key, label, inst)
        self.version_ok(entry, version, path, required)
        try:
            definition = (
                skill_definition(comp.path)
                if kind == "skill"
                else json.loads(comp.entry_file.read_text(encoding="utf-8"))
            )
            if kind == "skill" and inst and inst.payload.get("runtime_dependencies"):
                definition = {**definition, "dependencies": inst.payload["runtime_dependencies"]}
            if not isinstance(definition, dict):
                raise ValueError("definition must be object")
        except (OSError, ValueError):
            self.issue("definition_invalid", path, required)
            return
        self.nodes[label] = {
            "install_id": label,
            "kind": kind,
            "revision": comp.revision,
            "content_hash": component_hash(comp),
            "version": (
                str(version or "")
                if kind == "skill"
                else str(definition.get("version") or (inst.version if inst else "") or "")
            ),
            "platform_check": (
                "declared"
                if definition.get("platforms") or definition.get("platform")
                else "not_declared"
            ),
        }
        self.platform_ok(definition, path, required)
        children = _entries(definition, kind)
        if kind == "plugin" and not children:
            self.issue(
                "legacy_components_unknown",
                path,
                required,
                recovery_action="confirm_plugin_components",
            )
        for child in children:
            self.visit(child, comp.profile, path, required)

    def definition(self, definition, *, kind, profile, label):
        self.platform_ok(definition, [label], True)
        for entry in _entries(definition, kind):
            self.visit(entry, profile, [label])
        return self.report()

    def report(self):
        return {
            "ready": not self.errors,
            "errors": self.errors,
            "warnings": self.warnings,
            "nodes": list(self.nodes.values()),
        }


def check_installation(
    inst,
    *,
    user_id=None,
    available_mcp=None,
    available_kb=None,
    available_models=None,
    platform_name=None,
    bindings=None,
    runtime_versions=None,
):
    context = Context(
        user_id=user_id,
        bindings=bindings,
        available_mcp=set(available_mcp) if available_mcp is not None else None,
        available_kb=set(available_kb) if available_kb is not None else None,
        available_models=set(available_models) if available_models is not None else None,
        runtime_versions=runtime_versions,
    )
    if platform_name:
        context.platform_name = platform_name
    inspector = Inspector(context)
    inspector.visit({"kind": inst.kind, "id": inst.key}, inst.profile_id)
    return inspector.report()


def require_report(report):
    if not report["ready"]:
        raise DependencyMissing(
            "required capability dependencies are unavailable",
            details={
                "dependencies": report["errors"],
                "warnings": report["warnings"],
                "dependency_chain": report["errors"][0]["dependency_chain"],
            },
        )


def allowed_model_ids(user_id):
    """Reuse the live model gateway's role/user-switch authorization policy."""
    from core.db.models import ModelProvider
    from core.services.desktop_capability import _model_provider_allowed

    with registry._session() as db:
        return {
            str(row.provider_id)
            for row in db.query(ModelProvider)
            .filter(ModelProvider.is_active == True, ModelProvider.provider_type == "chat")
            .all()
            if _model_provider_allowed(db, str(user_id), row)
        }
