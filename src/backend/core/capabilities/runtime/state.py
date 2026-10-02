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
from typing import Any, Dict, Optional

from core.capabilities import registry, skills, store, view
from core.capabilities.errors import (
    CapabilityError,
    IntegrityFailed,
    PackageMissing,
    PermissionDenied,
    ViewUnavailable,
)
from core.capabilities.paths import BUILTIN_PROFILE, KIND_SKILL, LOCAL_PROFILE, require_root
from core.db.models import ContentBlock

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
    unavailable: dict = field(default_factory=dict)

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
            "unavailable": copy.deepcopy(self.unavailable),
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


def _validate_skill_binding(name: str, binding: dict, run: PreparedRun) -> None:
    """Re-check one frozen skill: still authorized (enabled/owner) and its bytes
    intact (content hash). Runs on every enumeration so a mid-session disable or
    an out-of-band edit of the frozen file is caught before a read.

    摘要走内容指纹缓存，缓存键是每个文件的元数据签名：任何字节改动都会让它失效并
    重新逐字节读一遍，所以篡改照样能抓到，而没变过的视图一次磁盘读都不用。
    """
    if binding["profile"] != BUILTIN_PROFILE:
        inst = registry.get(binding["install_id"])
        if inst is None or inst.state == "removed" or not inst.enabled:
            raise PermissionDenied("capability is no longer authorized", runtime_name=name)
        owner = inst.payload.get("owner_user_id")
        if owner and str(owner) != run.user_id:
            raise PermissionDenied("capability belongs to another user", runtime_name=name)
    comp = _component(binding)
    for iid in binding.get("dependency_install_ids", []):
        if iid.split(":", 2)[1] == BUILTIN_PROFILE:
            continue
        dependency = registry.get(iid)
        if dependency is None or dependency.state == "removed" or not dependency.enabled:
            raise PermissionDenied("skill dependency is no longer authorized", runtime_name=name)
        owner = dependency.payload.get("owner_user_id")
        if owner and str(owner) != run.user_id:
            raise PermissionDenied("skill dependency belongs to another user", runtime_name=name)
    if comp is None or skills.skill_dir_hash(comp.path) != binding["content_hash"]:
        raise IntegrityFailed("prepared revision is missing or changed", runtime_name=name)


def validate(
    run: PreparedRun,
    *,
    user_id: Optional[str] = None,
    execution_plane: str = "local",
    only_skill: Optional[str] = None,
) -> None:
    """Verify a prepared run is still authorized and its frozen bytes intact.

    带 ``only_skill`` 时只复查那一个技能绑定加上廉价的账号守卫。技能枚举按这个
    参数逐个复查自己那一份：N 次工作量，而不是每个加载器都把全量技能重算一遍的
    N² ——后者曾是桌面端装配最大的一块开销。整轮的视图校验在 ``rebuild`` 里做。
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
    # 不带 ``only_skill`` 时到此为止：组件级授权由读取方与执行视图各自把关，
    # 被撤销的单个组件只摘掉自己，不牵连这一轮里无关的工具或纯文本。
    if only_skill is None:
        return
    latest = get(run.run_id, scope_id=run.scope_id) or run
    if only_skill in latest.unavailable:
        raise PackageMissing("skill is unavailable in this view", runtime_name=only_skill)
    binding = run.bindings.get(only_skill)
    if binding is None:
        raise PermissionDenied("capability is no longer authorized", runtime_name=only_skill)
    _validate_skill_binding(only_skill, binding, run)


def rebuild(run: PreparedRun) -> Path:
    """把执行视图链到冻结的版本目录上。

    视图是派生物，随时可以从绑定重建；这里只创建和删除链接，绝不复制字节。
    某个组件通不过校验就把它从视图里摘掉并记进 ``unavailable``，其余照常可用。
    """
    run = get(run.run_id, scope_id=run.scope_id) or run
    fingerprint = _view_fingerprint(run)
    unavailable = dict(run.unavailable)
    targets: Dict[str, Path] = {}
    for name, binding in run.bindings.items():
        if name in unavailable:
            continue
        try:
            _validate_skill_binding(name, binding, run)
            component = _component(binding)
            if component is None:
                raise IntegrityFailed("prepared revision is missing or changed", runtime_name=name)
            targets[name] = component.path
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[name] = getattr(exc, "code", "view_unavailable")
    report = view.build_view(run.view_dir, targets, allowed_roots=[require_root()])
    if report.blocked or report.foreign:
        # 名字被真实目录占用或大小写撞名：沙箱绝不能收到一个来路不明的目录。
        raise ViewUnavailable(
            "prepared view is blocked",
            details={"blocked": report.blocked, "foreign": report.foreign},
        )
    if unavailable != run.unavailable:
        run = save(replace(run, unavailable=unavailable))
    with _view_build_lock:
        _view_built[(run.run_id, run.scope_id)] = fingerprint
    return run.view_dir


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


def save(run: PreparedRun) -> PreparedRun:
    """Persist the run snapshot; the business DB row is its durable record."""
    with registry._session() as db:
        key = _PREFIX + _snapshot_key(run.run_id, run.scope_id)
        row = db.get(ContentBlock, key)
        if row is None:
            db.add(ContentBlock(id=key, payload=run.to_dict()))
        else:
            row.payload = run.to_dict()
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
    from core.agent_skills.backends import CompositeBackend, FilesystemBackend
    from core.agent_skills.loader import MultiSourceSkillLoader

    _ensure_view(run)
    # 视图重建可能刚摘掉某个组件，读取方必须拿到落库后的最新 ``unavailable``。
    run = get(run.run_id, scope_id=run.scope_id) or run
    loader = MultiSourceSkillLoader(CompositeBackend([FilesystemBackend(run.view_dir, "prepared")]))
    loader.capability_run = run
    return loader


def view_for_execution(run_id: str, user_id: str, scope_id: str = "") -> Optional[Path]:
    run = get(run_id, scope_id=scope_id)
    if run is None:
        return None
    validate(run, user_id=user_id)
    return rebuild(run)


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
