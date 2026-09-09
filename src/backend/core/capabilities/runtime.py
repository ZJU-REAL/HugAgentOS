"""Durable desktop run bindings and per-run skill views in the existing business DB.

Snapshots pin full content hashes and revisions. They are retained with history;
there is deliberately no automatic revision collector until retention policy is
explicit. Authorization is rechecked before exposing the frozen files.
"""

from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from contextlib import contextmanager
import hashlib
import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

from core.db.models import ContentBlock

from . import archive, registry, skills, store, view
from .errors import IntegrityFailed, NameConflict, PackageMissing, PermissionDenied, ViewUnavailable
from .paths import BUILTIN_PROFILE, KIND_SKILL, LOCAL_PROFILE, require_root, revision_for_hash

_lock = threading.RLock()
_PREFIX = "desktop_capability_run:"


@dataclass
class _AssemblyPass:
    identity: tuple
    bindings: Optional[dict] = None
    mcps: Optional[dict] = None
    pending_view: Optional[tuple] = None
    enabled: bool = True


_assembly_pass = ContextVar("executor_capability_assembly", default=None)


@contextmanager
def executor_assembly(run_id, user_id, scope_id=""):
    """Internal factory transaction: publish only after the final full verification.

    No global verification cache: the token follows this task's worker calls and
    is invalidated on success, failure or cancellation. Independent API calls
    keep their normal validation behavior.
    """
    assembly = _AssemblyPass((str(run_id), str(user_id), str(scope_id or "")))
    token = _assembly_pass.set(assembly)
    try:
        yield
    finally:
        assembly.enabled = False
        _assembly_pass.reset(token)


def _assembly_for(run):
    assembly = _assembly_pass.get()
    if assembly is None or not assembly.enabled:
        return None
    if assembly.identity != (run.run_id, run.user_id, run.scope_id):
        raise IntegrityFailed("capability assembly belongs to another run")
    if assembly.bindings is not None and assembly.bindings != run.bindings:
        raise IntegrityFailed("capability bindings changed during assembly")
    if assembly.mcps is not None and assembly.mcps != run.mcp_bindings:
        raise IntegrityFailed("connector bindings changed during assembly")
    return assembly


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


def _validate_skill_binding(
    name: str, binding: dict, run: PreparedRun, *, fresh: bool = True, installed=None
) -> None:
    """Re-check one frozen skill: still authorized (enabled/owner) and its bytes
    intact (content hash). Runs on every enumeration so a mid-session disable or
    an out-of-band edit of the frozen file is caught before a read.

    ``fresh=True`` re-reads every file from disk — the authoritative sweep at
    view-build time and in direct recovery checks. ``fresh=False`` (the
    per-enumeration hot path) reuses the content-addressed hash cache keyed by
    the per-file metadata signature; changes to any frozen file invalidate
    the cached hash, so tampering is still
    caught while an unchanged view costs no disk reads.
    """
    if binding["profile"] != BUILTIN_PROFILE:
        inst = (
            installed.get(binding["install_id"])
            if installed is not None
            else registry.get(binding["install_id"])
        )
        if inst is None or inst.state == "removed" or not inst.enabled:
            raise PermissionDenied("capability is no longer authorized", runtime_name=name)
        owner = inst.payload.get("owner_user_id")
        if owner and str(owner) != run.user_id:
            raise PermissionDenied("capability belongs to another user", runtime_name=name)
    comp = _component(binding)
    if comp is None or skills.skill_dir_hash(comp.path, fresh=fresh) != binding["content_hash"]:
        raise IntegrityFailed("prepared revision is missing or changed", runtime_name=name)


def _check_skill_bindings(run, *, fresh, installed):
    def check(item):
        name, binding = item
        _validate_skill_binding(name, binding, run, fresh=fresh, installed=installed)

    # Windows metadata I/O dominates large closures. Each worker only reads its
    # own package and the detached registry snapshot; no DB session is shared.
    # Consume every result before exposing the view; any failure still rejects it.
    if os.name == "nt" and len(run.bindings) >= 8:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cap-verify") as pool:
            futures = [
                pool.submit(copy_context().run, check, item) for item in run.bindings.items()
            ]
            for future in futures:
                future.result()
    else:
        for item in run.bindings.items():
            check(item)


def validate(
    run: PreparedRun,
    *,
    user_id: Optional[str] = None,
    execution_plane: str = "local",
    only_skill: Optional[str] = None,
    fresh: bool = True,
) -> None:
    """Verify a prepared run is still authorized and its frozen bytes intact.

    With ``only_skill`` set, only that single skill binding is re-checked
    (authorization + fresh content hash) plus the cheap account guards. The
    per-assembly skill enumeration passes it so each of N skill loaders verifies
    just its own skill: O(N) work instead of the O(N²) full-run re-hash that
    dominated desktop agent setup (every loader re-hashing every skill, twice
    per assembly). The connector and cross-dependency loops, and the whole-run
    integrity sweep, still run at view-build time (``rebuild``) and whenever the
    default full ``validate`` is called (recovery probes, direct checks).
    """
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
        if only_skill is None and (
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
    if only_skill is not None:
        binding = run.bindings.get(only_skill)
        if binding is None:
            raise PermissionDenied("capability is no longer authorized", runtime_name=only_skill)
        _validate_skill_binding(only_skill, binding, run, fresh=False)
        return
    checked = {
        sid: binding["install_id"].split(":", 2)[1:]
        for sid, binding in run.mcp_bindings.items()
        if binding.get("authorization_checked")
    }
    cloud_ids = [
        sid for sid, (profile, _) in checked.items() if profile not in (LOCAL_PROFILE, "local-json")
    ]
    cloud_current = {}
    if cloud_ids:
        from core.services.desktop_cloud_bridge import cloud_gateway_mcp_configs

        cloud_current = cloud_gateway_mcp_configs(cloud_ids)
    local_current = None
    for server_id, (profile, key) in checked.items():
        binding = run.mcp_bindings[server_id]
        if profile == "local-json":
            from .mcp_json import local_server_configs

            current = local_server_configs().get(key)
        elif profile == LOCAL_PROFILE:
            if local_current is None:
                from core.services.mcp_service import McpServerConfigService

                service = McpServerConfigService.get_instance()
                local_current = {
                    **service.get_all_servers(enabled_only=True),
                    **service.get_owned_servers(run.user_id, enabled_only=True),
                }
            current = local_current.get(key)
        else:
            current = cloud_current.get(server_id)
        if not current or _config_digest(current) != binding["config_digest"]:
            raise PermissionDenied(
                "prepared connector is disabled or its connection instructions changed",
                runtime_name=server_id,
            )
    # One query for the whole closure: a full device re-checks 200+ rows here on
    # every message, and a lookup per row was half a second of round trips.
    installed = registry.get_many(
        [
            *(
                node["install_id"]
                for node in run.dependency_report.get("nodes", [])
                if node["kind"] != "skill"
            ),
            *(
                binding["install_id"]
                for binding in run.bindings.values()
                if binding["profile"] != BUILTIN_PROFILE
            ),
        ]
    )

    def check_dependency(node):
        from .dependency import component_hash

        kind, profile, key = node["install_id"].split(":", 2)
        inst = installed.get(node["install_id"])
        if not inst or not inst.enabled or inst.state == "removed":
            raise PermissionDenied("prepared dependency is no longer authorized")
        owner = inst.payload.get("owner_user_id")
        if owner and str(owner) != run.user_id:
            raise PermissionDenied("prepared dependency belongs to another user")
        comp = store.get(kind, profile, key, node["revision"])
        if comp is None or component_hash(comp, fresh=fresh) != node["content_hash"]:
            raise IntegrityFailed("prepared dependency revision is missing or changed")

    nodes = [node for node in run.dependency_report.get("nodes", []) if node["kind"] != "skill"]
    if os.name == "nt" and len(nodes) >= 8:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cap-dependency-verify") as pool:
            futures = [pool.submit(copy_context().run, check_dependency, node) for node in nodes]
            for future in futures:
                future.result()
    else:
        for node in nodes:
            check_dependency(node)
    _check_skill_bindings(run, fresh=fresh, installed=installed)


def rebuild(run: PreparedRun) -> Path:
    fingerprint = _view_fingerprint(run)
    assembly = _assembly_for(run)
    if assembly is None:
        validate(run)

    def target_for(item):
        name, binding = item
        component = _component(binding)
        if component is None:
            raise IntegrityFailed("prepared revision is missing or changed", runtime_name=name)
        return name, component.path

    if os.name == "nt" and len(run.bindings) >= 8:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cap-view-targets") as pool:
            futures = [
                pool.submit(copy_context().run, target_for, item) for item in run.bindings.items()
            ]
            targets = dict(future.result() for future in futures)
    else:
        targets = dict(target_for(item) for item in run.bindings.items())
    report = view.build_view(run.view_dir, targets, allowed_roots=[require_root()])
    if report.blocked:
        raise ViewUnavailable("prepared view is blocked", details={"blocked": report.blocked})
    if assembly is None:
        with _view_build_lock:
            _view_built[(run.run_id, run.scope_id)] = fingerprint
    else:
        assembly.pending_view = fingerprint
    return run.view_dir


def _freeze_candidate(name, candidate, *, installed=None, deferred=False):
    # An installed immutable identity can be pinned before reading its bytes.
    # The factory's final fresh sweep must prove it before any loader sees it.
    actual = (
        candidate.content_hash
        if deferred
        and candidate.profile != BUILTIN_PROFILE
        and candidate.content_hash
        and candidate.revision
        else skills.skill_dir_hash(candidate.path, fresh=True)
    )
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
    installation = None
    if profile != BUILTIN_PROFILE:
        installation = (
            registry.get(candidate.install_id)
            if installed is None
            else installed.get(candidate.install_id)
        )
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
    inspector = Inspector(
        Context(
            user_id=str(user_id),
            bindings=lookup,
            installations={
                row.install_id: row for row in registry.list_installations(include_removed=True)
            },
            collect_hashes=False,  # Discovery returns identities only; final validation owns hashes.
        )
    )
    inspector.visit_roots(
        [({"kind": "skill", "id": name}, profile or LOCAL_PROFILE) for name in selected]
    )
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
            assembly = _assembly_pass.get()
            if assembly is not None:
                assembly.enabled = False  # Recovery retains the standalone checks.
            validate(previous, user_id=user_id, execution_plane=execution_plane, fresh=False)
            missing = set(skill_ids or []) - set(previous.bindings)
            if missing:
                raise PackageMissing(
                    "requested skills are absent from the frozen run",
                    details={"skills": sorted(missing)},
                )
            _ensure_view(previous)
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
        local_candidates = {
            name: candidate
            for name, candidate in res.chosen.items()
            if candidate.profile in (LOCAL_PROFILE, BUILTIN_PROFILE)
        }
        installed = registry.get_many(
            candidate.install_id
            for candidate in local_candidates.values()
            if candidate.profile != BUILTIN_PROFILE
        )
        assembly = _assembly_pass.get()
        deferred = bool(assembly and assembly.enabled)
        if deferred and assembly.identity != (str(run_id), str(user_id), str(scope_id or "")):
            raise IntegrityFailed("capability assembly belongs to another run")

        def freeze(item):
            name, candidate = item
            return name, _freeze_candidate(name, candidate, installed=installed, deferred=deferred)

        if os.name == "nt" and len(local_candidates) >= 8:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cap-freeze") as pool:
                pending = [
                    pool.submit(copy_context().run, freeze, item)
                    for item in local_candidates.items()
                ]
                bindings = dict(future.result() for future in pending)
        else:
            bindings = dict(freeze(item) for item in local_candidates.items())
        selected = _selected_skill_closure(
            res, bindings, skill_ids or [], user_id, initial_profile, agent_definition, plugin_ids
        )
        for name in selected:
            candidate = res.chosen.get(name)
            if candidate is not None and name not in bindings:
                bindings[name] = _freeze_candidate(name, candidate, deferred=deferred)
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
        if deferred:
            assembly.bindings = copy.deepcopy(run.bindings)
        rebuild(run)
        with registry._session() as db:
            db.add(
                ContentBlock(id=_PREFIX + _snapshot_key(run_id, scope_id), payload=run.to_dict())
            )
        return run


_view_build_lock = threading.Lock()
# (run_id, scope_id) -> fingerprint of the frozen skill set last materialized.
# The frozen store is content-addressed, so an unchanged fingerprint means the
# junction view on disk is already correct; rebuilding it (deep validate + all
# junctions) on every agent assembly was pure per-session waste. A capability
# change re-resolves to new revisions → new fingerprint → rebuild.
_view_built: dict[tuple[str, str], tuple] = {}


def _view_fingerprint(run: PreparedRun) -> tuple:
    return (
        skills.view_generation(),
        tuple(sorted((name, b.get("revision")) for name, b in run.bindings.items())),
    )


def _ensure_view(run: PreparedRun) -> None:
    """Materialize the run's view only when its frozen skill set actually moved."""
    key = (run.run_id, run.scope_id)
    fingerprint = _view_fingerprint(run)
    with _view_build_lock:
        already = _view_built.get(key) == fingerprint
    if not already or not run.view_dir.exists():
        rebuild(run)


def frozen_loader(run: PreparedRun):
    if _assembly_for(run) is not None:
        raise ViewUnavailable("capability assembly has not completed verification")
    from core.agent_skills.backends import CompositeBackend, FilesystemBackend
    from core.agent_skills.loader import MultiSourceSkillLoader

    _ensure_view(run)
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
        assembly = _assembly_for(saved or run)
        if assembly is None:
            validate(saved or run)
        pinned = (saved or run).mcp_bindings
        if not (saved or run).mcp_frozen:
            pinned = current
            frozen = replace(saved or run, mcp_bindings=pinned, mcp_frozen=True)
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
        if assembly is not None:
            assembly.mcps = copy.deepcopy(pinned)
        # Pinned schemas are persisted and never edited afterwards; sharing them
        # with the assembled config is safe and avoids re-copying every tool schema.
        return {
            sid: {
                **config,
                **{
                    key: pinned[sid][key]
                    for key in ("manifest_tools", "schema_hash", "manifest_revision")
                    if pinned[sid].get(key) is not None
                },
            }
            for sid, config in configs.items()
        }


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


def _persist_preflight_report(run, report, *, offered=()):
    """Freeze identity once ready while continuing to publish current readiness.

    ``offered`` names the nodes that only the ambient catalog contributed. A
    ready scope must never swap the closure it *committed* to, but the catalog
    it offers may legitimately grow — a skill whose connector arrived becomes
    usable — so a newcomer is pinned rather than treated as tampering.
    """
    with _lock:
        latest = get(run.run_id, scope_id=run.scope_id) or run
        prior = latest.dependency_report
        frozen = bool(prior.get("frozen") or prior.get("ready"))
        changed = False
        report = copy.deepcopy(report)
        if frozen:
            offered = set(offered)
            pinned = {node["install_id"]: node for node in prior.get("nodes", [])}
            for node in report.get("nodes", []):
                previous = pinned.get(node["install_id"])
                if previous is None:
                    if node["install_id"] not in offered:
                        changed = True
                    else:
                        pinned[node["install_id"]] = node
                elif any(
                    previous.get(field) != node.get(field)
                    for field in ("kind", "revision", "content_hash")
                ):
                    changed = True
            report["nodes"] = copy.deepcopy(list(pinned.values()))
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
    progressive.visit_roots(
        [
            ({"kind": node["kind"], "id": node["install_id"]}, node["install_id"].split(":", 2)[1])
            for node in plugin_nodes
        ]
    )
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


_catalog_lock = threading.Lock()
# (signal) -> {skill runtime name: (errors, nodes, warnings)}. The verdict for a
# menu item is a pure function of the signal below, and a conversation asks the
# same question again on every message.
_catalog_probes: Dict[tuple, Dict[str, tuple]] = {}


def _catalog_signal(run, context) -> tuple:
    return (
        run.user_id,
        registry.generation(),
        skills.view_generation(),
        frozenset(context.available_mcp or ()),
        frozenset(context.available_kb or ()),
        frozenset(context.available_models) if context.available_models is not None else None,
        tuple(
            sorted(
                (node["install_id"], node.get("revision"), node.get("content_hash"))
                for node in run.dependency_report.get("nodes", [])
            )
        ),
    )


def preflight(
    run,
    *,
    skill_ids=(),
    catalog_skill_ids=(),
    agent_definition=None,
    plugin_ids=(),
    available_mcp=(),
    available_kb=(),
    available_models=None,
    plugin_nodes=(),
):
    """Stop before connecting/executing tools if the authorized closure is incomplete.

    ``skill_ids``, ``plugin_ids`` and the agent definition are what this turn
    *committed* to — the user selected them, the model already invoked them, or
    the bound sub-agent declares them. An unmet dependency there stops the turn.

    ``catalog_skill_ids`` is the ambient menu the model may pick from. A menu
    item whose own dependencies are unmet on this device is taken off the menu
    for this turn and reported under ``unavailable_skills``; it never stops the
    turn. One catalog skill missing a connector must not make the whole
    assistant unusable.
    """
    from .dependency import Context, Inspector, allowed_model_ids, require_report

    run = get(run.run_id, scope_id=run.scope_id) or run
    from .errors import CapabilityError

    assembly = _assembly_for(run)
    try:
        if assembly is None:
            validate(run, fresh=False)
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
        # The assembly's final fresh gate verifies these identities once. The
        # graph walk reads definitions/constraints but need not rehash them.
        pinned_hashes=(
            {
                **{
                    binding["install_id"]: (binding["revision"], binding["content_hash"])
                    for binding in run.bindings.values()
                },
                **{
                    node["install_id"]: (node["revision"], node["content_hash"])
                    for node in plugin_nodes or []
                },
            }
            if assembly is not None
            else None
        ),
        installations={
            row.install_id: row for row in registry.list_installations(include_removed=True)
        },
    )
    inspector = Inspector(context)
    committed = list(dict.fromkeys(skill_ids or []))
    for name in committed:
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
    committed_nodes = set(inspector.nodes)
    # Each menu item is inspected in its own walker so an unmet dependency is
    # attributable to exactly one skill; only the ones that can actually run
    # here are offered to the model and contribute their identity to the run.
    unavailable = []
    usable_skill_ids = list(committed)
    committed_set = set(committed)
    signal = _catalog_signal(run, context)
    with _catalog_lock:
        remembered = dict(_catalog_probes.get(signal) or {})
    missing_catalog = [
        name
        for name in dict.fromkeys(catalog_skill_ids or [])
        if name not in committed_set and name not in remembered
    ]

    def inspect_catalog(name):
        probe = Inspector(context)
        probe.visit({"kind": "skill", "id": name}, LOCAL_PROFILE)
        return name, (probe.errors, probe.nodes, probe.warnings)

    if os.name == "nt" and len(missing_catalog) >= 8:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cap-catalog") as pool:
            futures = [
                pool.submit(copy_context().run, inspect_catalog, name) for name in missing_catalog
            ]
            for future in futures:
                name, seen = future.result()
                remembered[name] = seen
    for name in dict.fromkeys(catalog_skill_ids or []):
        if name in committed_set:
            continue
        seen = remembered.get(name)
        if seen is None:
            probe = Inspector(context)
            probe.visit({"kind": "skill", "id": name}, LOCAL_PROFILE)
            seen = (probe.errors, probe.nodes, probe.warnings)
            remembered[name] = seen
        errors, nodes, warnings = seen
        if errors:
            unavailable.append({"skill_id": name, "reasons": copy.deepcopy(errors)})
            continue
        inspector.nodes.update(copy.deepcopy(nodes))
        inspector.warnings.extend(copy.deepcopy(warnings))
        usable_skill_ids.append(name)
    offered_nodes = set(inspector.nodes) - committed_nodes
    report = inspector.report()
    report["unavailable_skills"] = unavailable
    for row in unavailable:
        report["warnings"].extend(row["reasons"])
    if plugin_nodes:
        _merge_progressive_recheck(report, plugin_nodes, context, usable_skill_ids, available_mcp)
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
    _assembly_for(updated)
    # Always fetch live grants and read every byte at this final factory gate.
    validate(updated, fresh=assembly is not None)
    updated = _persist_preflight_report(updated, report, offered=offered_nodes)
    require_report(updated.dependency_report)
    with _catalog_lock:
        _catalog_probes.clear()
        _catalog_probes[signal] = remembered
    if assembly is not None:
        _assembly_for(updated)
        if assembly.pending_view == _view_fingerprint(updated):
            with _view_build_lock:
                _view_built[(updated.run_id, updated.scope_id)] = assembly.pending_view
        assembly.enabled = False
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
