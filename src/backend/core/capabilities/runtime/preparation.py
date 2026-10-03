"""Resolve and freeze user-selected capability revisions."""

from __future__ import annotations

import copy

from core.capabilities import archive, registry, skills, store
from core.capabilities.errors import CapabilityError, IntegrityFailed, PackageMissing
from core.capabilities.paths import BUILTIN_PROFILE, KIND_SKILL, LOCAL_PROFILE, revision_for_hash
from core.capabilities.runtime import state as runtime


def _freeze_candidate(name, candidate):
    # 摘要走内容指纹缓存：候选路径是不可变存储里的版本目录，字节一改元数据签名
    # 就变、缓存随之失效，所以复用安全，而重复冻结同一版本不必再读一遍磁盘。
    actual = skills.skill_dir_hash(candidate.path)
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
        installation = registry.get(candidate.install_id)
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


def prepare(
    run_id: str,
    user_id: str,
    *,
    skill_ids=None,
    execution_plane="local",
    agent_definition=None,
    plugin_ids=(),
    scope_id: str = "",
) -> runtime.PreparedRun:
    """冻结这一轮可用的能力闭包。

    冻结不是复制：每个组件都钉在不可变存储里的一个版本上，版本按内容指纹划分且
    永不原地修改，所以编辑安装目录只会产生新版本，已冻结的这一轮看到的字节自始
    至终不变。准备不了的单个组件记进 ``unavailable``，不影响其余组件。
    """
    from core.capabilities.preparation import ensure_cloud_ready

    if not run_id:
        raise ValueError("a run id is required for a durable capability snapshot")
    previous = runtime.get(run_id, scope_id=scope_id)
    if previous is not None:
        runtime.validate(previous, user_id=user_id, execution_plane=execution_plane)
        # 重放这一轮只能用它当初冻下来的那些；子智能体想要更多，得开新的一轮，
        # 不能借同一个 run 键把闭包悄悄撑大。
        missing = set(skill_ids or []) - set(previous.bindings) - set(previous.unavailable)
        if missing:
            raise PackageMissing(
                "requested skills are absent from the frozen run",
                details={"skills": sorted(missing)},
            )
        runtime.rebuild(previous)
        return runtime.get(run_id, scope_id=scope_id) or previous
    if execution_plane != "local":
        raise PackageMissing("device capabilities require local execution")
    requested = list(
        dict.fromkeys([*(skill_ids or []), *(getattr(agent_definition, "skill_ids", None) or [])])
    )
    # 这一轮的根：选中的技能，加上选中的插件。先各自按需下载，再从它们出发
    # 走一遍依赖，把闭包里牵连到的技能也收进来。一个根准备不了只记下它自己。
    account = skills.current_account_profile() or LOCAL_PROFILE
    roots = [("skill", name, LOCAL_PROFILE, name) for name in requested]
    roots += [("plugin", key, account, "plugin:" + key) for key in plugin_ids]
    unavailable = {}
    for kind, key, _profile, label in roots:
        keys = {"skill_keys": [key]} if kind == "skill" else {"plugin_keys": [key]}
        try:
            ensure_cloud_ready(user_id, **keys)
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[label] = getattr(exc, "code", "package_missing")
    resolution = skills.resolve_for_user(str(user_id))
    from core.capabilities.dependency import Context
    from core.capabilities.readiness import _BindingInspector, _choice_bindings

    discovery = _BindingInspector(
        Context(
            user_id=str(user_id), collect_hashes=False, bindings=_choice_bindings(resolution.chosen)
        )
    )
    for kind, key, profile, _label in roots:
        try:
            discovery.visit({"kind": kind, "id": key}, profile)
        except (CapabilityError, OSError, ValueError):
            continue
    if agent_definition is not None:
        discovery.definition(
            _definition_data(agent_definition),
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
    for name in requested:
        candidate = resolution.chosen.get(name)
        if candidate is None:
            unavailable[name] = (
                "name_conflict" if name in resolution.conflicts else "package_missing"
            )
            continue
        try:
            bindings[name] = _freeze_candidate(name, candidate)
            unavailable.pop(name, None)
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[name] = getattr(exc, "code", "package_missing")
    run = runtime.PreparedRun(
        str(run_id),
        str(user_id),
        None,
        execution_plane,
        bindings,
        scope_id=str(scope_id or ""),
        unavailable=unavailable,
    )
    if any(b["profile"] not in (LOCAL_PROFILE, BUILTIN_PROFILE) for b in bindings.values()):
        run = runtime._bind_cloud_identity(run)
    runtime.save(run)
    runtime.rebuild(run)
    return runtime.get(run_id, scope_id=scope_id) or run
