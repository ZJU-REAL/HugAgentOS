"""Read-only management readiness, separate from retained package publication state."""

from __future__ import annotations

import shutil
from . import registry, store
from .dependency import Context, Inspector, allowed_model_ids, skill_definition
from .errors import CapabilityError
from .paths import LOCAL_PROFILE


def context_for_user(user_id, *, available_mcp=()):
    try:
        models = allowed_model_ids(user_id)
    except Exception:
        models = None
    try:
        from core.auth.kb_permissions import get_accessible_local_kb_ids

        with registry._session() as db:
            kbs = get_accessible_local_kb_ids(db, user_id)
    except Exception:
        kbs = None
    from . import skills

    choices = skills.resolve_for_user(user_id).chosen
    return Context(
        user_id=user_id,
        bindings=_choice_bindings(choices),
        available_mcp=set(available_mcp),
        available_models=models,
        available_kb=kbs,
    )


def _result(report, *, components=()):
    return {
        "ready": report["ready"],
        "missing_required": [
            error["reason"] + ": " + error["dependency_chain"][-1] for error in report["errors"]
        ],
        "components": list(components),
        "errors": report["errors"],
        "warnings": report["warnings"],
        "nodes": report["nodes"],
        "dependency_report": report,
    }


def _verify_report_hashes(inspector):
    for node in list(inspector.nodes.values()):
        inst = registry.get(node["install_id"])
        expected = (
            (inst.payload.get("resolved_content_hash") or inst.content_hash) if inst else None
        )
        if expected and expected != node["content_hash"]:
            inspector.issue("content_integrity_failed", [node["install_id"]])


def file_readiness(inst, context):
    inspector = _BindingInspector(context, root_installation=inst)
    try:
        inspector.visit({"kind": inst.kind, "id": inst.install_id}, inst.profile_id)
        _verify_report_hashes(inspector)
    except (OSError, ValueError, CapabilityError):
        inspector.issue("definition_invalid", [inst.install_id])
    report = inspector.report()
    components = []
    if inst.kind == "plugin":
        for iid, required in registry.components_of(inst.install_id).items():
            kind, _, key = iid.split(":", 2)
            item = registry.get(iid)
            ready = bool(item and item.ready and item.enabled)
            if kind == "mcp":
                ready = key in (context.available_mcp or set())
            labels = {iid, kind + ":" + key}
            if any(
                labels.intersection(issue["dependency_chain"])
                for issue in report["errors"] + report["warnings"]
            ):
                ready = False
            components.append(
                {"install_id": iid, "kind": kind, "key": key, "required": required, "ready": ready}
            )
    return _result(report, components=components)


def builtin_readiness(candidate, context):
    inspector = _BindingInspector(context)
    try:
        inspector.definition(
            skill_definition(candidate.path),
            kind="skill",
            profile=LOCAL_PROFILE,
            label=candidate.install_id,
        )
        _verify_report_hashes(inspector)
    except (OSError, ValueError, CapabilityError):
        inspector.issue("definition_invalid", [candidate.install_id])
    return _result(inspector.report())


def connector_readiness(candidate, config):
    inspector = Inspector(Context())
    if not candidate.usable:
        inspector.issue(candidate.state, [candidate.install_id])
    if candidate.source != "cloud":
        config = config or {}
        inspector.definition(
            config, kind="mcp", profile=candidate.profile, label=candidate.install_id
        )
        if config.get("transport", "stdio") == "stdio":
            command = str(config.get("command") or "")
            if not command or not shutil.which(command):
                inspector.issue("runtime_command_missing", [candidate.install_id])
    return _result(inspector.report())


def files_ready(inst):
    return bool(
        inst.ready and store.get(inst.kind, inst.profile_id, inst.key, inst.resolved_revision)
    )


def apply_to_item(entry, readiness, *, downloaded=None):
    entry["readiness"] = readiness
    if downloaded is not None:
        entry["files_ready"] = bool(downloaded)
    entry["usable"] = bool(entry.get("usable") and readiness["ready"])
    if not readiness["ready"] and entry.get("resolution", {}).get("outcome") == "chosen":
        entry["resolution"] = {"outcome": "unusable", "reason": "dependency_missing"}
    return entry


def _choice_bindings(choices):
    return {
        name: {
            "install_id": candidate.install_id,
            "revision": candidate.revision,
            "_candidate": candidate,
        }
        for name, candidate in choices.items()
    }


def _candidate_component(candidate):
    inst = registry.get(candidate.install_id)
    component = store.StoredComponent(
        candidate.kind,
        candidate.profile,
        candidate.install_id.split(":", 2)[2],
        candidate.revision or "",
        candidate.path,
    )
    return candidate.install_id, inst, component


class _BindingInspector(Inspector):
    """Evaluate the exact root while children use the caller's source decisions."""

    def __init__(self, context, *, root_installation=None, root_candidate=None, intrinsic=False):
        super().__init__(context)
        self.root_installation = root_installation
        self.root_candidate = root_candidate
        self.intrinsic = intrinsic
        self._root_resolution = False

    def visit(self, entry, profile, chain=(), required=True):
        if self.intrinsic and chain and entry.get("kind") in ("skill", "agent", "plugin"):
            return  # The source decision for these children has not been made yet.
        previous = self._root_resolution
        self._root_resolution = not chain
        try:
            return super().visit(entry, profile, chain, required)
        finally:
            self._root_resolution = previous

    def resolve(self, kind, profile, key):
        if self._root_resolution:
            if self.root_candidate is not None:
                return _candidate_component(self.root_candidate)
            if self.root_installation is not None:
                inst = self.root_installation
                comp = (
                    store.get(inst.kind, inst.profile_id, inst.key, inst.resolved_revision)
                    if inst.ready
                    else None
                )
                return inst.install_id, inst, comp
        if kind == "skill" and self.context.bindings is not None:
            if ":" in key:
                parts = key.split(":", 2)
                if len(parts) != 3 or parts[0] != kind:
                    return None, None, None
                from . import skills
                from .paths import BUILTIN_PROFILE

                if parts[1] not in (LOCAL_PROFILE, BUILTIN_PROFILE) and (
                    parts[1] != skills.current_account_profile()
                    or not skills.account_authorized_for(self.context.user_id)
                ):
                    return None, None, None
                key = parts[2]
            binding = self.context.bindings.get(key)
            if binding is not None and binding.get("_candidate") is not None:
                return _candidate_component(binding["_candidate"])
        return super().resolve(kind, profile, key)


class _SelectionInspector(_BindingInspector):
    """Check local eligibility before per-run MCP/model/KB grants are bound."""

    def external(self, entry, chain, required):
        if entry.get("kind") in ("mcp", "model", "kb"):
            self.platform_ok(entry, chain, required)
            return
        super().external(entry, chain, required)


def eligible_skill_candidates(candidates, user_id, *, preferences=None, requested=None):
    """Use pure source decisions, never re-enter the public resolver or mutate state.

    First exclude roots with intrinsic platform/runtime failures. Then evaluate
    children against the same chosen names runtime will freeze. Re-evaluate from
    intrinsic eligibility when a bad source disappears so parents can recover
    through the compatible alternative. Oscillating source/dependency graphs are
    conservatively pruned until no selected node depends on an excluded node.
    """
    from dataclasses import replace
    from .resolver import resolve

    candidates = list(candidates)
    preferences = (
        registry.preferences("skill", user_id=user_id) if preferences is None else preferences
    )

    def check(candidate, choices, *, intrinsic=False):
        if not candidate.usable or candidate.path is None:
            return candidate
        inspector = _SelectionInspector(
            Context(user_id=user_id, bindings=_choice_bindings(choices)),
            root_candidate=candidate,
            intrinsic=intrinsic,
        )
        try:
            inspector.visit({"kind": "skill", "id": candidate.install_id}, candidate.profile)
        except (OSError, ValueError, CapabilityError):
            inspector.issue("definition_invalid", [candidate.install_id])
        return replace(candidate, usable=not inspector.errors)

    intrinsic = [check(candidate, {}, intrinsic=True) for candidate in candidates]
    current = intrinsic
    seen = []
    for _ in range(2 * len(candidates) + 2):
        state = tuple(candidate.usable for candidate in current)
        if state in seen:
            # Cyclic alternatives cannot establish a stable runnable source.
            current = [
                replace(candidate, usable=all(flags[i] for flags in seen))
                for i, candidate in enumerate(current)
            ]
            break
        seen.append(state)
        choices = resolve("skill", current, preferences=preferences, requested=requested).chosen
        following = [check(candidate, choices) for candidate in intrinsic]
        if tuple(candidate.usable for candidate in following) == state:
            return following
        current = following
    # Monotone final pruning after an unstable graph/bounded iteration limit.
    for _ in range(len(candidates) + 1):
        choices = resolve("skill", current, preferences=preferences, requested=requested).chosen
        following = [check(candidate, choices) for candidate in current]
        if [candidate.usable for candidate in following] == [
            candidate.usable for candidate in current
        ]:
            return following
        current = following
    return current
