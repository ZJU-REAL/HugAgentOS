"""Durable desktop run bindings and per-run skill views in the existing business DB.

Snapshots pin full content hashes and revisions. They are retained with history;
there is deliberately no automatic revision collector until retention policy is
explicit. Authorization is rechecked before exposing the frozen files.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from core.db.models import ContentBlock

from . import archive, registry, skills, store, view
from .errors import IntegrityFailed, NameConflict, PackageMissing, PermissionDenied, ViewUnavailable
from .paths import BUILTIN_PROFILE, KIND_SKILL, LOCAL_PROFILE, require_root, revision_for_hash

_lock = threading.RLock()
_PREFIX = "desktop_capability_run:"


def _key(run_id):
    return hashlib.sha256(str(run_id).encode()).hexdigest()


def child_scope(parent_scope: str, kind: str, *identities: str) -> str:
    """Bounded, deterministic scope from durable orchestration identities."""
    if not kind or not identities or any(not str(value or "").strip() for value in identities):
        raise IntegrityFailed("capability scope requires durable invocation identities")
    material = json.dumps(
        [str(parent_scope or ""), str(kind), *map(str, identities)], separators=(",", ":")
    )
    return "s_" + _key(material)


def _snapshot_key(run_id, scope_id=""):
    # Preserve every existing main-run ContentBlock and view key byte-for-byte.
    return (
        _key(run_id)
        if not scope_id
        else _key(json.dumps([str(run_id), str(scope_id)], separators=(",", ":")))
    )


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    user_id: str
    profile: Optional[str]
    execution_plane: str
    bindings: dict[str, dict[str, Any]]
    mcp_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_frozen: bool = False
    authorization_fingerprint: Optional[str] = None
    dependency_report: dict = field(default_factory=dict)
    scope_id: str = ""

    @property
    def view_dir(self):
        return (
            require_root()
            / ".capabilities"
            / "views"
            / _snapshot_key(self.run_id, self.scope_id)
            / "skills"
        )

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "scope_id": self.scope_id,
            "user_id": self.user_id,
            "profile": self.profile,
            "execution_plane": self.execution_plane,
            "bindings": copy.deepcopy(self.bindings),
            "mcp_bindings": copy.deepcopy(self.mcp_bindings),
            "mcp_frozen": self.mcp_frozen,
            "authorization_fingerprint": self.authorization_fingerprint,
            "dependency_report": copy.deepcopy(self.dependency_report),
        }


def get(run_id: str, scope_id: str = "") -> Optional[PreparedRun]:
    with registry._session() as db:
        row = db.get(ContentBlock, _PREFIX + _snapshot_key(run_id, scope_id))
        return PreparedRun(**copy.deepcopy(row.payload)) if row else None


def _component(binding):
    return store.get(KIND_SKILL, binding["profile"], binding["key"], binding["revision"])


def validate(
    run: PreparedRun, *, user_id: Optional[str] = None, execution_plane: str = "local"
) -> None:
    if user_id is not None and run.user_id != str(user_id):
        raise PermissionDenied("prepared run belongs to another user")
    if run.execution_plane != execution_plane:
        raise PackageMissing(
            "prepared execution plane changed; prepare a new run",
            details={"recovery_action": "switch_execution_plane"},
        )
    if run.profile is not None:
        from core.services.desktop_cloud_bridge import (
            _state_fingerprint,
            ensure_current_authorization,
            get_state,
        )

        if (
            run.profile != skills.current_account_profile()
            or run.authorization_fingerprint != _state_fingerprint(get_state())
        ):
            raise PermissionDenied("cloud session changed; prepare a new run")
        if (
            any(b["profile"] not in (BUILTIN_PROFILE, LOCAL_PROFILE) for b in run.bindings.values())
            or any(
                b["install_id"].split(":", 2)[1] not in (LOCAL_PROFILE, "local-json")
                for b in run.mcp_bindings.values()
            )
            or any(
                node["install_id"].split(":", 2)[1] not in (LOCAL_PROFILE, BUILTIN_PROFILE)
                for node in run.dependency_report.get("nodes", [])
            )
        ):
            ensure_current_authorization()
    for server_id, binding in run.mcp_bindings.items():
        if not binding.get("authorization_checked"):
            continue
        _, profile, key = binding["install_id"].split(":", 2)
        if profile == "local-json":
            from .mcp_json import local_server_configs

            current = local_server_configs().get(key)
        elif profile == LOCAL_PROFILE:
            from core.services.mcp_service import McpServerConfigService

            service = McpServerConfigService.get_instance()
            current = {
                **service.get_all_servers(enabled_only=True),
                **service.get_owned_servers(run.user_id, enabled_only=True),
            }.get(key)
        else:
            from core.services.desktop_cloud_bridge import cloud_gateway_mcp_configs

            current = cloud_gateway_mcp_configs([server_id]).get(server_id)
        if not current or _config_digest(current) != binding["config_digest"]:
            raise PermissionDenied(
                "prepared connector is disabled or its connection instructions changed",
                runtime_name=server_id,
            )
    for node in run.dependency_report.get("nodes", []):
        if node["kind"] == "skill":
            continue
        from .dependency import component_hash

        kind, profile, key = node["install_id"].split(":", 2)
        inst = registry.get(node["install_id"])
        if not inst or not inst.enabled or inst.state == "removed":
            raise PermissionDenied("prepared dependency is no longer authorized")
        owner = inst.payload.get("owner_user_id")
        if owner and str(owner) != run.user_id:
            raise PermissionDenied("prepared dependency belongs to another user")
        comp = store.get(kind, profile, key, node["revision"])
        if comp is None or component_hash(comp) != node["content_hash"]:
            raise IntegrityFailed("prepared dependency revision is missing or changed")
    for name, binding in run.bindings.items():
        if binding["profile"] != BUILTIN_PROFILE:
            inst = registry.get(binding["install_id"])
            if inst is None or inst.state == "removed" or not inst.enabled:
                raise PermissionDenied("capability is no longer authorized", runtime_name=name)
            owner = inst.payload.get("owner_user_id")
            if owner and str(owner) != run.user_id:
                raise PermissionDenied("capability belongs to another user", runtime_name=name)
        comp = _component(binding)
        if comp is None or skills.skill_dir_hash(comp.path, fresh=True) != binding["content_hash"]:
            raise IntegrityFailed("prepared revision is missing or changed", runtime_name=name)


def rebuild(run: PreparedRun) -> Path:
    validate(run)
    targets = {name: _component(binding).path for name, binding in run.bindings.items()}
    report = view.build_view(run.view_dir, targets, allowed_roots=[require_root()])
    if report.blocked:
        raise ViewUnavailable("prepared view is blocked", details={"blocked": report.blocked})
    return run.view_dir


def _freeze_candidate(name, candidate):
    actual = skills.skill_dir_hash(candidate.path, fresh=True)
    revision = candidate.revision or revision_for_hash(actual)
    profile = candidate.profile
    if profile == BUILTIN_PROFILE:
        if store.get(KIND_SKILL, profile, name, revision) is None:
            files = {
                rel: path.read_bytes()
                for rel, path in archive.iter_files(candidate.path)
                if rel != ".inventory.json"
            }
            store.write_from_files(KIND_SKILL, profile, name, revision, files)
    elif candidate.content_hash and actual != candidate.content_hash:
        raise IntegrityFailed("installed content changed", runtime_name=name)
    installation = registry.get(candidate.install_id) if profile != BUILTIN_PROFILE else None
    return {
        "install_id": candidate.install_id,
        "profile": profile,
        "key": candidate.ref.key if candidate.ref else candidate.install_id.split(":", 2)[2],
        "revision": revision,
        "content_hash": actual,
        "resource_ref": candidate.ref.to_dict() if candidate.ref else None,
        "version": installation.version if installation else "",
    }


def _definition_data(definition):
    if hasattr(definition, "to_serialized"):
        return definition.to_serialized()
    return {
        key: copy.deepcopy(getattr(definition, key, None))
        for key in (
            "skill_ids",
            "mcp_server_ids",
            "plugin_ids",
            "kb_ids",
            "model_provider_id",
            "extra_config",
            "dependencies",
            "platforms",
            "extensions",
        )
    }


def _selected_skill_closure(
    resolution, local_bindings, selected, user_id, profile, agent_definition, plugin_ids
):
    """Discover selected files before freezing; actual grants are checked in preflight.

    This visits only declarations reached from this run. Unrelated cached cloud
    files must never turn a local run into a cloud-dependent run.
    """
    from .dependency import Context, Inspector

    lookup = {
        name: {"install_id": candidate.install_id, "revision": candidate.revision}
        for name, candidate in resolution.chosen.items()
    }
    lookup.update(local_bindings)
    inspector = Inspector(Context(user_id=str(user_id), bindings=lookup))
    for name in selected:
        inspector.visit({"kind": "skill", "id": name}, profile or LOCAL_PROFILE)
    for key in plugin_ids:
        inspector.visit({"kind": "plugin", "id": key}, profile or LOCAL_PROFILE)
    if agent_definition is not None:
        inspector.definition(
            _definition_data(agent_definition),
            kind="agent",
            profile=getattr(agent_definition, "profile", LOCAL_PROFILE),
            label="agent:" + agent_definition.agent_id,
        )
    visited = set(inspector.nodes)
    return set(selected) | {
        name for name, candidate in resolution.chosen.items() if candidate.install_id in visited
    }


def _bind_cloud_identity(run):
    from core.services.desktop_cloud_bridge import (
        _state_fingerprint,
        ensure_current_authorization,
        get_state,
    )

    if not skills.account_authorized_for(run.user_id):
        raise PermissionDenied("cloud capabilities belong to another user")
    profile = skills.current_account_profile()
    fingerprint = _state_fingerprint(get_state())
    if not profile:
        raise PermissionDenied("cloud session is unavailable")
    if run.profile is not None and (
        run.profile != profile or run.authorization_fingerprint != fingerprint
    ):
        raise PermissionDenied("cloud session changed; prepare a new run")
    ensure_current_authorization()
    return replace(run, profile=profile, authorization_fingerprint=fingerprint)


def prepare(
    run_id: str,
    user_id: str,
    *,
    skill_ids=None,
    execution_plane="local",
    agent_definition=None,
    plugin_ids=(),
    scope_id: str = "",
) -> PreparedRun:
    if not run_id:
        raise ValueError("a run id is required for a durable capability snapshot")
    with _lock:
        previous = get(run_id, scope_id=scope_id)
        if previous is not None:
            validate(previous, user_id=user_id, execution_plane=execution_plane)
            missing = set(skill_ids or []) - set(previous.bindings)
            if missing:
                raise PackageMissing(
                    "requested skills are absent from the frozen run",
                    details={"skills": sorted(missing)},
                )
            rebuild(previous)
            return previous
        if execution_plane != "local":
            raise PackageMissing(
                "device capabilities require local execution",
                details={"recovery_action": "switch_execution_plane"},
            )
        initial_profile = skills.current_account_profile()
        from .preparation import ensure_cloud_ready

        # 选中的插件、被委派的智能体绑定的云端技能/插件及其组件按需下载，再解析。
        _agent_skill_keys = list(getattr(agent_definition, "skill_ids", None) or [])
        _agent_plugin_keys = list(getattr(agent_definition, "plugin_ids", None) or [])
        ensure_cloud_ready(
            user_id,
            skill_keys=[*(skill_ids or []), *_agent_skill_keys],
            plugin_keys=[*(plugin_ids or []), *_agent_plugin_keys],
        )
        res = skills.resolve_for_user(str(user_id))
        conflicts = set(skill_ids or []) & set(res.conflicts)
        if conflicts:
            raise NameConflict(
                "choose a source for the conflicting skills", details={"skills": sorted(conflicts)}
            )
        missing = set(skill_ids or []) - set(res.chosen)
        if missing:
            ensure_cloud_ready(user_id, skill_keys=sorted(missing))
            res = skills.resolve_for_user(str(user_id))
            missing = set(skill_ids or []) - set(res.chosen)
        if missing:
            raise PackageMissing(
                "selected skills are not ready", details={"skills": sorted(missing)}
            )
        # Local/builtin progressive reads stay available offline. Cloud content
        # is frozen only when selected directly or by an agent/plugin dependency.
        bindings = {
            name: _freeze_candidate(name, candidate)
            for name, candidate in res.chosen.items()
            if candidate.profile in (LOCAL_PROFILE, BUILTIN_PROFILE)
        }
        selected = _selected_skill_closure(
            res, bindings, skill_ids or [], user_id, initial_profile, agent_definition, plugin_ids
        )
        for name in selected:
            candidate = res.chosen.get(name)
            if candidate is not None and name not in bindings:
                bindings[name] = _freeze_candidate(name, candidate)
        run = PreparedRun(
            str(run_id), str(user_id), None, execution_plane, bindings, scope_id=str(scope_id or "")
        )
        if (
            any(
                binding["profile"] not in (LOCAL_PROFILE, BUILTIN_PROFILE)
                for binding in bindings.values()
            )
            or getattr(agent_definition, "origin", "local") == "cloud"
        ):
            run = _bind_cloud_identity(run)
        rebuild(run)
        with registry._session() as db:
            db.add(
                ContentBlock(id=_PREFIX + _snapshot_key(run_id, scope_id), payload=run.to_dict())
            )
        return run


def frozen_loader(run: PreparedRun):
    from core.agent_skills.backends import CompositeBackend, FilesystemBackend
    from core.agent_skills.loader import MultiSourceSkillLoader

    rebuild(run)
    loader = MultiSourceSkillLoader(CompositeBackend([FilesystemBackend(run.view_dir, "prepared")]))
    loader.capability_run = run
    return loader


def view_for_execution(run_id: str, user_id: str, scope_id: str = "") -> Optional[Path]:
    run = get(run_id, scope_id=scope_id)
    if run is None:
        return None
    validate(run, user_id=user_id)
    return rebuild(run)


def _config_digest(config):
    # Connection instructions are frozen; secret inputs may rotate independently.
    secret_names = (
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "API_KEY",
        "APIKEY",
        "CREDENTIAL",
        "AUTHORIZATION",
        "COOKIE",
    )

    def public_values(values):
        return {
            key: value
            for key, value in (values or {}).items()
            if not any(marker in str(key).upper().replace("-", "_") for marker in secret_names)
        }

    safe = {
        key: val
        for key, val in config.items()
        if key not in {"headers", "env", "manifest_tools", "manifest_revision", "schema_hash"}
    }
    safe["headers"] = public_values(config.get("headers"))
    safe["env"] = public_values(config.get("env"))
    return hashlib.sha256(json.dumps(safe, sort_keys=True, default=str).encode()).hexdigest()


def bind_mcp(run: PreparedRun, configs, choices):
    """Freeze MCP source identities/contracts; refresh credentials independently."""
    from .connectors import server_id_of

    selected = {server_id_of(c): c.install_id for c in choices.chosen.values()} if choices else {}
    current = {
        sid: {
            "install_id": selected.get(sid, "mcp:local:" + sid),
            "authorization_checked": sid in selected,
            "config_digest": _config_digest(config),
            "manifest_tools": copy.deepcopy(config.get("manifest_tools")),
            "schema_hash": config.get("schema_hash"),
            "manifest_revision": config.get("manifest_revision"),
        }
        for sid, config in configs.items()
    }
    with _lock:
        saved = get(run.run_id, scope_id=run.scope_id)
        validate(saved or run)
        pinned = (saved or run).mcp_bindings
        if not (saved or run).mcp_frozen:
            pinned = current
            frozen = replace(saved or run, mcp_bindings=copy.deepcopy(pinned), mcp_frozen=True)
            if any(
                entry["install_id"].split(":", 2)[1] not in (LOCAL_PROFILE, "local-json")
                for entry in pinned.values()
            ):
                frozen = _bind_cloud_identity(frozen)
            with registry._session() as db:
                row = db.get(ContentBlock, _PREFIX + _snapshot_key(run.run_id, run.scope_id))
                row.payload = frozen.to_dict()
        else:
            # A sub-agent may use a subset; expanding outside the parent's
            # frozen bindings requires a new run, not an implicit source swap.
            for sid, entry in current.items():
                old = pinned.get(sid)
                if (
                    old is None
                    or old["install_id"] != entry["install_id"]
                    or old["config_digest"] != entry["config_digest"]
                ):
                    raise IntegrityFailed(
                        "connector binding changed; prepare a new run", runtime_name=sid
                    )
        output = copy.deepcopy(configs)
        for sid in output:
            for key in ("manifest_tools", "schema_hash", "manifest_revision"):
                if pinned[sid].get(key) is not None:
                    output[sid][key] = copy.deepcopy(pinned[sid][key])
        return output


def pin_agent_definition(run_id, user_id, definition, *, scope_id: str = ""):
    """Keep the selected agent's instructions and dependency IDs stable on replay."""
    from .agents import _JSON_FIELDS, AgentDefinition

    ident = str(definition.agent_id)
    key = "desktop_capability_agent:" + _snapshot_key(str(run_id) + ":" + ident, scope_id)
    profile = (
        skills.current_account_profile()
        if getattr(definition, "origin", "local") == "cloud"
        else None
    )
    from core.services.desktop_cloud_bridge import (
        _state_fingerprint,
        ensure_current_authorization,
        get_state,
    )

    fingerprint = _state_fingerprint(get_state()) if profile else None
    if getattr(definition, "origin", "local") == "cloud":
        if not skills.account_authorized_for(user_id):
            raise PermissionDenied("cloud agent belongs to another user")
        ensure_current_authorization()
        current = registry.get(
            registry.install_id("agent", str(getattr(definition, "profile", profile)), ident)
        )
        if current is None or not current.ready or not current.enabled:
            raise PermissionDenied("cloud agent is no longer authorized")
    if not getattr(definition, "is_enabled", True):
        raise PermissionDenied("selected agent is disabled")
    with _lock, registry._session() as db:
        row = db.get(ContentBlock, key)
        if row is not None:
            data = copy.deepcopy(row.payload)
            if (
                data["user_id"] != str(user_id)
                or data["profile"] != profile
                or data.get("authorization_fingerprint") != fingerprint
            ):
                raise PermissionDenied("prepared agent belongs to another account")
            return AgentDefinition.from_serialized(data["definition"])
        fields = {
            name: copy.deepcopy(getattr(definition, name, None))
            for name in _JSON_FIELDS
            if hasattr(definition, name)
        }
        fields.update(
            {
                "system_prompt": str(getattr(definition, "system_prompt", "") or ""),
                "user_id": getattr(definition, "user_id", None),
                "origin": getattr(definition, "origin", "local"),
                "profile": getattr(definition, "profile", LOCAL_PROFILE),
                "revision": getattr(definition, "revision", None),
            }
        )
        frozen = AgentDefinition.from_serialized(fields)
        db.add(
            ContentBlock(
                id=key,
                payload={
                    "run_id": str(run_id),
                    "scope_id": str(scope_id or ""),
                    "user_id": str(user_id),
                    "profile": profile,
                    "authorization_fingerprint": fingerprint,
                    "definition": fields,
                },
            )
        )
        return frozen


def references(kind: str, profile: str, key: str, revision: Optional[str] = None) -> list[str]:
    """History retains exact skill, plugin and agent revisions until explicitly purged."""
    target = registry.install_id(kind, profile, key)
    with registry._session() as db:
        rows = db.query(ContentBlock).filter(ContentBlock.id.startswith(_PREFIX)).all()
        result = []
        for row in rows:
            entries = list((row.payload.get("bindings") or {}).values()) + (
                row.payload.get("dependency_report") or {}
            ).get("nodes", [])
            if any(
                entry.get("install_id") == target
                and (revision is None or entry.get("revision") == revision)
                for entry in entries
            ):
                result.append(str(row.payload["run_id"]))
        if kind == "agent":
            rows = (
                db.query(ContentBlock)
                .filter(ContentBlock.id.startswith("desktop_capability_agent:"))
                .all()
            )
            for row in rows:
                data = row.payload.get("definition") or {}
                if (
                    data.get("agent_id") == key
                    and data.get("profile", LOCAL_PROFILE) == profile
                    and (revision is None or data.get("revision") == revision)
                ):
                    if row.payload.get("run_id"):
                        result.append(str(row.payload["run_id"]))
        return sorted(set(result))


def _persist_preflight_report(run, report):
    """Freeze identity once ready while continuing to publish current readiness."""
    with _lock:
        latest = get(run.run_id, scope_id=run.scope_id) or run
        prior = latest.dependency_report
        frozen = bool(prior.get("frozen") or prior.get("ready"))
        changed = False
        report = copy.deepcopy(report)
        if frozen:
            nodes = {node["install_id"]: node for node in prior.get("nodes", [])}
            for node in report.get("nodes", []):
                previous = nodes.get(node["install_id"])
                if previous is None or any(
                    previous.get(field) != node.get(field)
                    for field in ("kind", "revision", "content_hash")
                ):
                    changed = True
            report["nodes"] = copy.deepcopy(prior.get("nodes", []))
        if changed:
            report["ready"] = False
            report.setdefault("errors", []).append(
                {
                    "code": "scope_selection_changed",
                    "dependency_chain": [],
                    "recovery_action": "prepare_new_scope",
                }
            )
        report["frozen"] = frozen or bool(report.get("ready"))
        report["state"] = "ready" if report.get("ready") else "blocked"
        if not report.get("ready"):
            report["error"] = {
                "code": "integrity_failed" if changed else "dependency_missing",
                "recovery_action": "prepare_new_scope" if changed else "inspect_dependencies",
            }
        updated = replace(latest, dependency_report=report)
        if latest.profile is None and run.profile is not None:
            updated = replace(
                updated,
                profile=run.profile,
                authorization_fingerprint=run.authorization_fingerprint,
            )
        with registry._session() as db:
            row = db.get(ContentBlock, _PREFIX + _snapshot_key(run.run_id, run.scope_id))
            row.payload = updated.to_dict()
        if changed:
            raise IntegrityFailed("capability selection changed; prepare a new scope")
        return updated


def _merge_progressive_recheck(report, plugin_nodes, context, skill_ids, available_mcp):
    """Recheck progressive plugin declarations against the frozen skill bindings.

    Only components this run did not select are skipped; version, platform and
    runtime constraints on the selected ones must survive the intersection, and
    a definition that moved between preparation and now is an integrity failure.
    """
    from .dependency import Inspector, _identifier

    selected_skills, selected_mcp = set(skill_ids or ()), set(available_mcp or ())
    expected_nodes = {node["install_id"]: node for node in plugin_nodes}
    progressive_context = replace(context, frozen_nodes={**context.frozen_nodes, **expected_nodes})

    def is_selected(entry, _required):
        key = _identifier(entry).split(":")[-1]
        if entry.get("kind") == "skill":
            return key in selected_skills
        if entry.get("kind") == "mcp":
            return key in selected_mcp
        return True

    progressive = Inspector(progressive_context, on_visit=is_selected)
    for node in plugin_nodes:
        kind, profile, _ = node["install_id"].split(":", 2)
        progressive.visit({"kind": kind, "id": node["install_id"]}, profile)
    checked = progressive.report()

    for node in checked["nodes"]:
        expected = expected_nodes.get(node["install_id"])
        if expected and any(
            node.get(field) != expected.get(field) for field in ("revision", "content_hash")
        ):
            raise IntegrityFailed("plugin definition changed during preparation")
    known = {node["install_id"] for node in report["nodes"]}
    report["nodes"].extend(node for node in checked["nodes"] if node["install_id"] not in known)
    report["errors"].extend(checked["errors"])
    report["warnings"].extend(checked["warnings"])
    report["ready"] = report["ready"] and checked["ready"]


def preflight(
    run,
    *,
    skill_ids=(),
    agent_definition=None,
    plugin_ids=(),
    available_mcp=(),
    available_kb=(),
    available_models=None,
    plugin_nodes=(),
):
    """Stop before connecting/executing tools if the authorized closure is incomplete."""
    from .dependency import Context, Inspector, allowed_model_ids, require_report

    run = get(run.run_id, scope_id=run.scope_id) or run
    from .errors import CapabilityError

    try:
        validate(run)
    except CapabilityError as exc:
        report = copy.deepcopy(run.dependency_report)
        report.update(
            ready=False,
            errors=[
                {"code": exc.code, "dependency_chain": [], "recovery_action": exc.recovery_action}
            ],
            warnings=[],
        )
        _persist_preflight_report(run, report)
        raise
    if available_models is None:
        try:
            available_models = allowed_model_ids(run.user_id)
        except Exception:
            available_models = None
    context = Context(
        user_id=run.user_id,
        bindings=run.bindings,
        available_mcp=set(available_mcp),
        available_kb=set(available_kb),
        available_models=available_models,
        frozen_nodes={node["install_id"]: node for node in run.dependency_report.get("nodes", [])},
    )
    inspector = Inspector(context)
    for name in skill_ids or []:
        inspector.visit({"kind": "skill", "id": name}, LOCAL_PROFILE)
    for name in plugin_ids or []:
        inspector.visit(
            {"kind": "plugin", "id": name},
            run.profile or skills.current_account_profile() or LOCAL_PROFILE,
        )
    if agent_definition is not None:
        definition = _definition_data(agent_definition)
        agent_profile = getattr(agent_definition, "profile", LOCAL_PROFILE)
        inspector.definition(
            definition,
            kind="agent",
            profile=agent_profile,
            label="agent:" + agent_definition.agent_id,
        )
        # Preserve a concrete definition revision when the selected agent has a
        # store projection, in addition to its already-frozen inline instructions.
        agent_iid = registry.install_id("agent", agent_profile, agent_definition.agent_id)
        inst = registry.get(agent_iid)
        if inst:
            inspector.visit({"kind": "agent", "id": inst.key}, inst.profile_id)
    report = inspector.report()
    if plugin_nodes:
        _merge_progressive_recheck(report, plugin_nodes, context, skill_ids, available_mcp)
    report["state"] = "ready" if report["ready"] else "blocked"
    if not report["ready"]:
        report["error"] = {"code": "dependency_missing", "recovery_action": "inspect_dependencies"}
    updated = replace(run, dependency_report=report)
    if report["ready"] and (
        any(
            node["install_id"].split(":", 2)[1] not in (LOCAL_PROFILE, BUILTIN_PROFILE)
            for node in report.get("nodes", [])
        )
        or getattr(agent_definition, "origin", "local") == "cloud"
    ):
        updated = _bind_cloud_identity(updated)
    validate(updated)
    updated = _persist_preflight_report(updated, report)
    require_report(updated.dependency_report)
    return updated


_TOOL_SCOPE_PREFIX = "desktop_capability_tool_scope:"


def record_tool_scope(run: PreparedRun, tool_call_id: str, tool_name: str) -> None:
    """Persist adapter ownership before its Intent; collisions never change scope."""
    if not tool_call_id or not tool_name:
        raise IntegrityFailed("tool capability scope requires a durable tool call identity")
    payload = {
        "run_id": run.run_id,
        "user_id": run.user_id,
        "scope_id": run.scope_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
    }
    key = _TOOL_SCOPE_PREFIX + _key(json.dumps([run.run_id, tool_call_id], separators=(",", ":")))
    with _lock, registry._session() as db:
        row = db.get(ContentBlock, key)
        if row is not None:
            if row.payload != payload:
                raise IntegrityFailed("tool call identity belongs to a different capability scope")
        else:
            db.add(ContentBlock(id=key, payload=payload))


def require_root_tool_scope(run_id: str, user_id: str, tool_call_id: str, tool_name: str) -> None:
    """The legacy recovery adapter can only reconstruct a proven root tool surface."""
    key = _TOOL_SCOPE_PREFIX + _key(json.dumps([run_id, tool_call_id], separators=(",", ":")))
    with registry._session() as db:
        row = db.get(ContentBlock, key)
        if row is not None:
            expected = {
                "run_id": run_id,
                "user_id": user_id,
                "scope_id": "",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
            }
            if row.payload != expected:
                raise IntegrityFailed("scoped tool recovery requires its original child executor")
            return
        for snapshot in db.query(ContentBlock).filter(ContentBlock.id.like(_PREFIX + "%")):
            data = snapshot.payload or {}
            if data.get("run_id") == run_id and data.get("scope_id"):
                raise IntegrityFailed("tool recovery has no proven capability scope")
