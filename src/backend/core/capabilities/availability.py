"""Fault isolation at the desktop capability boundary, outside the chat harness."""

from dataclasses import replace
import os
import uuid

from core.db.models import ContentBlock
from . import archive, registry, skills, view
from .errors import CapabilityError, PackageMissing
from .paths import (
    BUILTIN_PROFILE,
    LOCAL_PROFILE,
    assert_managed_path,
    require_root,
    revision_for_hash,
)


def _publish_copy(source, target):
    target = assert_managed_path(target)
    if target.exists():
        return target
    entries = [
        (rel, file, info)
        for rel, file, info in archive.iter_file_stats(source)
        if rel != ".inventory.json"
    ]
    if (
        len(entries) > archive.MAX_MEMBERS
        or sum(info.st_size for _, _, info in entries) > archive.MAX_TOTAL_BYTES
        or any(info.st_size > archive.MAX_MEMBER_BYTES for _, _, info in entries)
    ):
        raise PackageMissing("skill execution copy exceeds the allowed size")
    files = {rel: file.read_bytes() for rel, file, _ in entries}
    if "SKILL.md" not in files:
        raise PackageMissing("skill entry is missing")
    stage = assert_managed_path(target.parent / ("stage-" + uuid.uuid4().hex))
    try:
        archive.write_files(stage, files)
        if os.name != "nt":
            for rel, _, info in entries:
                (stage / rel).chmod(info.st_mode & 0o777)
        os.replace(stage, target)
    finally:
        if stage.exists():
            import shutil

            shutil.rmtree(stage)
    return target


def save(run):
    from . import runtime

    with registry._session() as db:
        key = runtime._PREFIX + runtime._snapshot_key(run.run_id, run.scope_id)
        row = db.get(ContentBlock, key)
        if row is None:
            db.add(ContentBlock(id=key, payload=run.to_dict()))
        else:
            row.payload = run.to_dict()


def prepare_available(
    run_id, user_id, *, skill_ids, execution_plane, agent_definition, plugin_ids, scope_id
):
    from . import runtime
    from .preparation import ensure_cloud_ready

    if not run_id:
        raise ValueError("a run id is required")
    assembly = runtime._assembly_pass.get()
    if assembly is not None:
        assembly.enabled = False
    previous = runtime.get(run_id, scope_id=scope_id)
    if previous is not None:
        if not previous.allow_unavailable:
            # Legacy recovery keeps its original bytes, never silently replays
            # an interrupted task against a newly edited installation.
            previous = replace(previous, allow_unavailable=True)
            runtime.validate(previous, user_id=user_id, execution_plane=execution_plane)
            if "connector_parents" not in previous.dependency_report and any(
                node.get("kind") == "plugin" for node in previous.dependency_report.get("nodes", [])
            ):
                # Old snapshots did not retain per-connector plugin grants.
                # A fresh task can prepare them; recovery cannot infer them.
                previous.unavailable.update(
                    {"mcp:" + sid: "legacy_binding_unavailable" for sid in previous.mcp_bindings}
                )
            for name, binding in previous.bindings.items():
                try:
                    runtime._validate_skill_binding(name, binding, previous)
                    target = _publish_copy(
                        runtime._component(binding).path,
                        require_root()
                        / ".capabilities"
                        / "snapshots"
                        / runtime._snapshot_key(run_id, scope_id)
                        / name,
                    )
                    binding["snapshot_path"] = str(target.relative_to(require_root()))
                except (CapabilityError, OSError, ValueError):
                    previous.unavailable[name] = "legacy_revision_unavailable"
            save(previous)
        runtime.validate(previous, user_id=user_id, execution_plane=execution_plane)
        runtime.rebuild(previous)
        return runtime.get(run_id, scope_id=scope_id) or previous
    if execution_plane != "local":
        raise PackageMissing("device capabilities require local execution")
    requested = list(
        dict.fromkeys([*(skill_ids or []), *(getattr(agent_definition, "skill_ids", None) or [])])
    )
    unavailable = {}
    for name in requested:
        try:
            ensure_cloud_ready(user_id, skill_keys=[name])
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[name] = getattr(exc, "code", "package_missing")
    for plugin in plugin_ids:
        try:
            ensure_cloud_ready(user_id, plugin_keys=[plugin])
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable["plugin:" + plugin] = getattr(exc, "code", "package_missing")
    resolution = skills.resolve_for_user(str(user_id))
    from .dependency import Context
    from .readiness import _BindingInspector, _choice_bindings

    discovery = _BindingInspector(
        Context(
            user_id=str(user_id), collect_hashes=False, bindings=_choice_bindings(resolution.chosen)
        )
    )
    for name in list(requested):
        try:
            discovery.visit({"kind": "skill", "id": name}, LOCAL_PROFILE)
        except (CapabilityError, OSError, ValueError):
            continue
    for plugin in plugin_ids:
        try:
            discovery.visit(
                {"kind": "plugin", "id": plugin}, skills.current_account_profile() or LOCAL_PROFILE
            )
        except (CapabilityError, OSError, ValueError):
            continue
    if agent_definition is not None:
        discovery.definition(
            runtime._definition_data(agent_definition),
            kind="agent",
            profile=getattr(agent_definition, "profile", LOCAL_PROFILE),
            label="agent:" + str(agent_definition.agent_id),
        )
    requested = list(
        dict.fromkeys(
            [
                *requested,
                *(
                    node["install_id"].split(":", 2)[2]
                    for node in discovery.nodes.values()
                    if node["kind"] == "skill"
                ),
            ]
        )
    )
    bindings = {}
    root = require_root() / ".capabilities" / "snapshots" / runtime._snapshot_key(run_id, scope_id)
    for name in requested:
        candidate = resolution.chosen.get(name)
        if candidate is None:
            unavailable[name] = (
                "name_conflict" if name in resolution.conflicts else "package_missing"
            )
            continue
        try:
            # The editable installation and the frozen task are different files.
            # Verify downloads at installation; local edits become a new task revision.
            target = _publish_copy(candidate.path, root / name)
            digest = skills.skill_dir_hash(target, fresh=True)
            bindings[name] = {
                "install_id": candidate.install_id,
                "profile": candidate.profile,
                "key": candidate.install_id.split(":", 2)[2],
                "revision": revision_for_hash(digest),
                "content_hash": digest,
                "snapshot_path": str(target.relative_to(require_root())),
                "resource_ref": candidate.ref.to_dict() if candidate.ref else None,
                "version": (
                    registry.get(candidate.install_id).version
                    if candidate.profile != BUILTIN_PROFILE
                    else ""
                ),
            }
            unavailable.pop(name, None)
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[name] = getattr(exc, "code", "package_missing")
    run = runtime.PreparedRun(
        str(run_id),
        str(user_id),
        None,
        execution_plane,
        bindings,
        scope_id=scope_id,
        allow_unavailable=True,
        unavailable=unavailable,
    )
    if any(b["profile"] not in (LOCAL_PROFILE, BUILTIN_PROFILE) for b in bindings.values()):
        run = runtime._bind_cloud_identity(run)
    save(run)
    rebuild_available_view(run)
    return runtime.get(run_id, scope_id=scope_id)


def rebuild_available_view(run):
    from . import runtime

    run = runtime.get(run.run_id, scope_id=run.scope_id) or run
    targets = {}
    unavailable = dict(run.unavailable)
    for name, binding in run.bindings.items():
        if name in unavailable:
            continue
        try:
            runtime._validate_skill_binding(name, binding, run, fresh=False)
            component = runtime._component(binding)
            target = (
                require_root()
                / ".capabilities"
                / "execution"
                / runtime._snapshot_key(run.run_id, run.scope_id)
                / name
            )
            targets[name] = _publish_copy(component.path, target)
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[name] = getattr(exc, "code", "view_unavailable")
    try:
        report = view.build_view(run.view_dir, targets, allowed_roots=[require_root()])
        # A second build repairs transient link failures without deleting real user directories.
        if report.blocked or report.foreign:
            report = view.build_view(run.view_dir, targets, allowed_roots=[require_root()])
        if report.blocked or report.foreign:
            # A foreign directory or busy link must never leak into the sandbox.
            # Preserve it and publish an independent view for this run instead.
            run = replace(run, view_revision="-" + uuid.uuid4().hex)
            report = view.build_view(run.view_dir, targets, allowed_roots=[require_root()])
            unavailable.update({name: "view_unavailable" for name in report.blocked})
            if report.blocked or report.foreign:
                run = replace(run, view_revision="-" + uuid.uuid4().hex)
                report = view.build_view(
                    run.view_dir,
                    {n: p for n, p in targets.items() if n not in unavailable},
                    allowed_roots=[require_root()],
                )
                if report.blocked or report.foreign:
                    raise PackageMissing("isolated execution view is unavailable")
    except (CapabilityError, OSError):
        unavailable.update({name: "view_unavailable" for name in targets})
        run = replace(run, view_revision="-" + uuid.uuid4().hex)
        # No stale or unverified directory is ever returned to the sandbox.
        report = view.build_view(run.view_dir, {}, allowed_roots=[require_root()])
        if report.blocked or report.foreign:
            raise PackageMissing("empty execution view is unavailable")
    save(replace(run, unavailable=unavailable))
    return run.view_dir


def preflight_available(
    run, *, available_mcp, available_kb, available_models, plugin_ids, agent_definition
):
    from . import runtime
    from .dependency import Context, Inspector

    runtime.validate(run)
    current = runtime.get(run.run_id, scope_id=run.scope_id) or run
    run = current
    if current.dependency_report.get("ready"):
        rebuild_available_view(current)
        return runtime.get(run.run_id, scope_id=run.scope_id) or current
    unavailable = dict(current.unavailable)
    context = Context(
        user_id=run.user_id,
        bindings=run.bindings,
        available_mcp=set(available_mcp),
        available_kb=set(available_kb),
        available_models=available_models,
        collect_hashes=False,
    )
    nodes = {}
    connector_parents = {}

    class AvailabilityInspector(Inspector):
        def external(self, entry, chain, required):
            if entry.get("kind") == "mcp":
                connector_parents.setdefault(chain[-1][4:], []).extend(
                    parent for parent in chain[:-1] if parent.startswith("plugin:")
                )
            return super().external(entry, chain, required)

    for name, binding in run.bindings.items():
        if name in unavailable:
            continue
        inspector = AvailabilityInspector(context)
        try:
            inspector.visit({"kind": "skill", "id": name}, binding["profile"])
            if inspector.errors:
                unavailable[name] = "dependency_missing"
            else:
                nodes.update(inspector.nodes)
                binding["dependency_install_ids"] = [
                    node["install_id"]
                    for node in inspector.nodes.values()
                    if node["install_id"] != binding["install_id"]
                ]
        except (CapabilityError, OSError, ValueError, AttributeError):
            unavailable[name] = "dependency_missing"
    for plugin in plugin_ids:
        inspector = AvailabilityInspector(context)
        try:
            inspector.visit({"kind": "plugin", "id": plugin}, run.profile or LOCAL_PROFILE)
            if inspector.errors:
                unavailable["plugin:" + plugin] = "dependency_missing"
            else:
                nodes.update(inspector.nodes)
            parents = [
                node["install_id"]
                for node in inspector.nodes.values()
                if node["kind"] in ("plugin", "agent")
            ]
            for node in inspector.nodes.values():
                name = node["install_id"].split(":", 2)[2]
                if node["kind"] == "skill" and name in run.bindings:
                    run.bindings[name].setdefault("dependency_install_ids", []).extend(parents)
                    if inspector.errors:
                        unavailable[name] = "dependency_missing"
        except (CapabilityError, OSError, ValueError):
            unavailable["plugin:" + plugin] = "dependency_missing"
    report = {
        "ready": True,
        "state": "ready",
        "nodes": list(nodes.values()),
        "errors": [],
        "warnings": [],
        "connector_parents": connector_parents,
        "unavailable_skills": [
            {"skill_id": name, "reasons": [{"code": code}]} for name, code in unavailable.items()
        ],
    }
    updated = replace(current, unavailable=unavailable, dependency_report=report)
    save(updated)
    rebuild_available_view(updated)
    return runtime.get(run.run_id, scope_id=run.scope_id) or updated
