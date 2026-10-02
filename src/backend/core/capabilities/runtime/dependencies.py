"""Check selected dependency closures and progressive activation."""

from __future__ import annotations

from dataclasses import replace

from core.capabilities import registry, store
from core.capabilities.errors import CapabilityError, IntegrityFailed, PermissionDenied
from core.capabilities.paths import BUILTIN_PROFILE, LOCAL_PROFILE
from core.capabilities.runtime import state as runtime


def validate_activation(run: runtime.PreparedRun, nodes, *, available_mcp=()) -> None:
    """激活一个此前被推迟的插件之前，复查它自己。

    延迟加载把插件的定义留到模型真正要用时才展开，这中间用户可能已经把它停用、
    卸载，或者定义文件被改过；它声明的版本 / 平台约束也要拿这一轮**冻结的那个**
    技能版本去对，而不是拿当前安装的版本。``nodes`` 是推迟那一刻记下的身份。
    只查这一个插件及其定义闭包，不牵连整轮。
    """
    from core.capabilities.dependency import (
        Context,
        Inspector,
        _identifier,
        component_hash,
        require_report,
    )

    nodes = list(nodes or [])
    if not nodes:
        return
    for node in nodes:
        kind, profile, key = node["install_id"].split(":", 2)
        if profile != BUILTIN_PROFILE:
            inst = registry.get(node["install_id"])
            if inst is None or inst.state == "removed" or not inst.enabled:
                raise PermissionDenied("capability is no longer authorized", runtime_name=key)
            owner = inst.payload.get("owner_user_id")
            if owner and str(owner) != run.user_id:
                raise PermissionDenied("capability belongs to another user", runtime_name=key)
        comp = store.get(kind, profile, key, node["revision"])
        if comp is None or component_hash(comp) != node["content_hash"]:
            raise IntegrityFailed(
                "prepared dependency revision is missing or changed", runtime_name=key
            )

    # 这一轮没选中的组件跳过；选中的那些，约束必须在冻结版本上依然成立。
    selected_mcp = set(available_mcp or ())

    def is_selected(entry, _required):
        key = _identifier(entry).split(":")[-1]
        if entry.get("kind") == "skill":
            return key in run.bindings
        if entry.get("kind") == "mcp":
            return key in selected_mcp
        return True

    inspector = Inspector(
        Context(
            user_id=run.user_id,
            bindings=run.bindings,
            available_mcp=selected_mcp,
            frozen_nodes={node["install_id"]: node for node in nodes},
            collect_hashes=False,
        ),
        on_visit=is_selected,
    )
    inspector.visit_roots(
        [
            ({"kind": node["kind"], "id": node["install_id"]}, node["install_id"].split(":", 2)[1])
            for node in nodes
        ]
    )
    require_report(inspector.report())


def preflight(
    run,
    *,
    available_mcp=(),
    available_kb=(),
    available_models=None,
    plugin_ids=(),
):
    """走一遍依赖闭包，把这一轮真正能跑的能力定下来。

    某个技能的依赖在本机不成立，就只把这个技能摘掉并记进 ``unavailable_skills``，
    绝不因此让整个助手不可用。连接器与插件的从属关系记进 ``connector_parents``，
    供「加载插件」一类的工具反查。
    """
    from core.capabilities.dependency import Context, Inspector

    runtime.validate(run)
    run = runtime.get(run.run_id, scope_id=run.scope_id) or run
    if run.dependency_report.get("ready"):
        # 这个作用域已经定过一次，就保持那份冻结报告，只把视图对齐。
        runtime.rebuild(run)
        return runtime.get(run.run_id, scope_id=run.scope_id) or run
    unavailable = dict(run.unavailable)
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

    class _ParentTrackingInspector(Inspector):
        def external(self, entry, chain, required):
            if entry.get("kind") == "mcp":
                connector_parents.setdefault(chain[-1][4:], []).extend(
                    parent for parent in chain[:-1] if parent.startswith("plugin:")
                )
            return super().external(entry, chain, required)

    for name, binding in run.bindings.items():
        if name in unavailable:
            continue
        inspector = _ParentTrackingInspector(context)
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
        inspector = _ParentTrackingInspector(context)
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
    updated = replace(run, unavailable=unavailable, dependency_report=report)
    runtime.save(updated)
    # 这里必须真重建：它顺带是这一轮唯一一次逐个复核冻结字节的机会，
    # 中途被改过的组件要在交给沙箱之前撤下来。不要图快改成按指纹跳过。
    runtime.rebuild(updated)
    return runtime.get(run.run_id, scope_id=run.scope_id) or updated
