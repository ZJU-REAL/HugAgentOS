"""桌面双端本机后端：能力清单由本机登记表投影，与本机自建条目合并。

登录时的同步把云端账号的四类能力写进 ``device_capability_installations``（见
:mod:`registry`），运行时解析读的也是它。界面若再去本机业务库读一遍，看到的就
不是真正生效的那份：云端同步来的条目在本机业务库里根本没有行，能力中心于是只
剩本机自带的内置项——这正是「登录后四类能力完全不一样」的由来。

所以本机后端的清单接口在这里把登记表投影成目录项：云端同步来的以登记表为准，
本机自建的仍以业务库为准，两者 id 不相交，各有唯一真源。启停统一写登记表，
读写同源。云端部署与纯本机形态下 :func:`active` 恒为假，本模块不参与。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from core.config.catalog_runtime import merge_items_by_id

from .paths import KIND_AGENT, KIND_PLUGIN, KIND_SKILL, capabilities_enabled, capability_root

logger = logging.getLogger(__name__)

# 目录按 id 覆盖合并的规则只有一套，这里跟着用同一份。
merge_items = merge_items_by_id


def active() -> bool:
    """本机后端已被壳孵化、且能力文件仓可用时，清单才由登记表投影。

    这也是「混合模式下能力一律以云端账号同步下来的那份为准」的唯一判据：为真时本机
    自带的内置技能与内置连接器整体不参与——既不进能力中心的清单，也不进装配，用户
    才不会看到云端账号里根本没有、点了也没反应的能力。
    """
    from core.auth.desktop_bridge import bridge_enabled

    return bool(bridge_enabled() and capabilities_enabled())


def _profile() -> Optional[str]:
    from core.capabilities import skills

    try:
        return skills.current_account_profile()
    except Exception as exc:  # noqa: BLE001 - 身份未就绪时清单为空，不是错误
        logger.debug("[device-catalog] account profile unavailable: %s", exc)
        return None


def _installations(kind: str) -> List[Any]:
    from . import registry

    profile = _profile()
    if not profile:
        return []
    return registry.list_installations(kind=kind, profile_id=profile)


def _managed_connectors() -> List[Dict[str, Any]]:
    from core.services.desktop_cloud_bridge import managed_connectors

    return managed_connectors()


# ── 技能正文 ──────────────────────────────────────────────────────────


@lru_cache(maxsize=256)
def _read_component(
    root: str, profile: str, key: str, revision: str
) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """(SKILL.md 正文, 其余文件的相对路径)；读不到返回 None。

    按 revision 缓存：revision 由内容哈希派生，内容一变就是另一个键，不需要额外
    的失效点。否则每打开一次能力中心，就把每个技能的目录重读一遍。``root`` 只为
    进键——换一个能力仓就是另一套文件。
    """
    from . import store

    component = store.get(KIND_SKILL, profile, key, revision)
    if component is None:
        return None
    try:
        body = component.entry_file.read_text(encoding="utf-8")
        files = tuple(
            sorted(
                path.relative_to(component.path).as_posix()
                for path in component.path.rglob("*")
                if path.is_file() and path.name not in ("SKILL.md", ".inventory.json")
            )
        )
    except OSError as exc:
        logger.warning("[device-catalog] skill files unreadable %s: %s", key, exc)
        return None
    return body, files


def _skill_body(inst) -> Optional[Tuple[str, Tuple[str, ...]]]:
    if not inst.ready:
        return None
    return _read_component(
        str(capability_root() or ""), inst.profile_id, inst.key, inst.resolved_revision
    )


# ── 目录叠加层 ────────────────────────────────────────────────────────


def _skill_item(inst) -> Dict[str, Any]:
    from core.config.catalog_loader import skill_body_from_raw

    description = inst.description or ""
    item = {
        "id": inst.key,
        "kind": "tool_bundle",
        "name": inst.display_name or inst.key,
        "description": description,
        "desc": description,
        "enabled": bool(inst.enabled),
        "version": inst.version or "1",
    }
    body = _skill_body(inst)
    # 详情读的是这台机器上那一版 SKILL.md 的正文——它才是这里真正会执行的内容。
    detail = skill_body_from_raw(body[0]) if body else ""
    if detail:
        item["detail"] = detail
    return item


def _connector_item(server: Dict[str, Any]) -> Dict[str, Any]:
    sid = str(server["server_id"])
    description = str(server.get("description") or "")
    item = {
        "id": sid,
        "kind": "mcp_server",
        "name": str(server.get("display_name") or sid),
        "description": description,
        "desc": description,
        "enabled": bool(server.get("enabled", True)),
        "version": "1",
        "config": {"server": sid},
    }
    icon = str(server.get("icon") or "")
    if icon:
        item["icon"] = icon
    return item


def _db_umbrella_item(members: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把数据库类连接器合并成云端展示的那一个「数据库查询」条目。

    ``query_database`` / ``db_query`` / ``es_query`` 在云端从来不单独露面：能力目录
    把它们收进一个伞形条目，按所连数据库类型在运行时选路（见
    ``agent_factory`` 对 ``DB_UMBRELLA_ID`` 的展开）。桌面端此前直接摊开云端下发的
    原始 server，于是同一份能力在网页端叫「数据库查询」、在桌面端叫「Elasticsearch
    查询」，用户会以为数据库工具没同步下来。

    启停取成员的并集：任一成员开着，伞形就是开着的。
    """
    from core.config.catalog_loader import (
        DB_UMBRELLA_DESC,
        DB_UMBRELLA_ICON,
        DB_UMBRELLA_ID,
        DB_UMBRELLA_NAME,
    )

    return {
        "id": DB_UMBRELLA_ID,
        "kind": "mcp_server",
        "name": DB_UMBRELLA_NAME,
        "description": DB_UMBRELLA_DESC,
        "desc": DB_UMBRELLA_DESC,
        "enabled": any(bool(m.get("enabled", True)) for m in members),
        "version": "1",
        "config": {"server": DB_UMBRELLA_ID},
        "icon": DB_UMBRELLA_ICON,
    }


def catalog_overlay() -> Dict[str, Any]:
    """能力目录在本机要叠加的那一层，一次取数算齐。

    - ``skills`` / ``mcp``：云端同步来的条目，停用的一并列出（开关要有地方可点）。
    - ``hidden_skills`` / ``hidden_mcp``：属于某个插件的组件 id。它们只在插件下
      露面，调用方要把这些 id 从技能库和连接器库里拿掉。

    归属两个来源都要：条目自己声明的 ``source_plugin``，以及插件登记的组件边。
    前者在插件清单还没同步下来时就已经生效——技能和插件是两次独立的同步，只靠
    插件那边反查会有一段空窗，用户就会看见插件的技能冒到技能库里；后者覆盖历史
    上还没有归属标记的条目。
    """
    if not active():
        return {"skills": [], "mcp": [], "hidden_skills": set(), "hidden_mcp": set()}
    skills = _installations(KIND_SKILL)
    connectors = _managed_connectors()
    hidden_skills = {inst.key for inst in skills if inst.source_plugin}
    hidden_mcp = {str(s["server_id"]) for s in connectors if s.get("source_plugin")}
    for inst in _installations(KIND_PLUGIN):
        components = dict(inst.payload.get("components") or {})
        hidden_skills.update(str(sid) for sid in components.get("skills") or [])
        hidden_mcp.update(str(mid) for mid in components.get("mcp") or [])
    from core.config.catalog_loader import DB_HIDDEN_SERVERS

    standalone = [s for s in connectors if not s.get("source_plugin")]
    db_members = [s for s in standalone if str(s.get("server_id")) in DB_HIDDEN_SERVERS]
    mcp_items = [
        _connector_item(s) for s in standalone if str(s.get("server_id")) not in DB_HIDDEN_SERVERS
    ]
    if db_members:
        mcp_items.append(_db_umbrella_item(db_members))
    return {
        "skills": [_skill_item(inst) for inst in skills if not inst.source_plugin],
        "mcp": mcp_items,
        "hidden_skills": hidden_skills,
        "hidden_mcp": hidden_mcp,
    }


# ── 智能体 ────────────────────────────────────────────────────────────


def _account_definitions() -> Dict[str, Any]:
    """账号下所有智能体定义，一次取齐。

    逐个 ``account_definition(id)`` 会把整份清单连同文件重查一遍，条目一多就是
    N+1 次查询加 N×M 次文件解析。
    """
    from . import agents as caps_agents

    try:
        return {defn.agent_id: defn for defn in caps_agents.account_definitions()}
    except Exception as exc:  # noqa: BLE001 - 文件未就绪时只展示登记信息
        logger.debug("[device-catalog] agent definitions unavailable: %s", exc)
        return {}


def _agent_entry(inst, definition) -> Dict[str, Any]:
    def field(name: str, fallback):
        return getattr(definition, name, fallback) if definition is not None else fallback

    return {
        "agent_id": inst.key,
        "owner_type": "user",
        "user_id": None,
        "name": inst.display_name or inst.key,
        "avatar": field("avatar", "") or "",
        "description": inst.description or "",
        "system_prompt": field("system_prompt", "") or "",
        "welcome_message": field("welcome_message", "") or "",
        "suggested_questions": list(field("suggested_questions", []) or []),
        "mcp_server_ids": list(field("mcp_server_ids", []) or []),
        "skill_ids": list(field("skill_ids", []) or []),
        "plugin_ids": list(field("plugin_ids", []) or []),
        "kb_ids": list(field("kb_ids", []) or []),
        "model_provider_id": field("model_provider_id", None),
        "temperature": field("temperature", None),
        "max_tokens": field("max_tokens", None),
        "max_iters": field("max_iters", None),
        "timeout": field("timeout", None),
        "is_enabled": bool(inst.enabled),
        "sort_order": field("sort_order", 0),
        "source_market_slug": field("source_market_slug", None),
        "ontology_tags": list(field("ontology_tags", []) or []),
        "extra_config": dict(field("extra_config", {}) or {}),
        "version": inst.version or "",
        "change_history": [],
        "created_at": None,
        "updated_at": None,
        "created_by": None,
    }


def agent_entries() -> List[Dict[str, Any]]:
    """云端账号的智能体投影成 ``/v1/agents`` 的条目结构。"""
    if not active():
        return []
    installations = _installations(KIND_AGENT)
    if not installations:
        return []
    definitions = _account_definitions()
    return [_agent_entry(inst, definitions.get(inst.key)) for inst in installations]


def toggle_projected_agent(agent_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """云端投影来的智能体只接受启停；返回 None 表示这条不是投影项。

    定义（提示词、绑定的技能与工具）是云端那一份，改它要去云端改完再同步；这台
    机器上能决定的只有开不开。
    """
    from . import registry

    if "is_enabled" not in data:
        return None
    profile = _profile()
    if not profile or not set_enabled(KIND_AGENT, agent_id, bool(data["is_enabled"])):
        return None
    inst = registry.get(registry.install_id(KIND_AGENT, profile, agent_id))
    if inst is None:
        return None
    return _agent_entry(inst, _account_definitions().get(agent_id))


# ── 插件 ──────────────────────────────────────────────────────────────


def plugin_entries() -> List[Dict[str, Any]]:
    """云端账号的插件投影成 ``/v1/plugins/installed`` 的条目结构。"""
    if not active():
        return []
    entries: List[Dict[str, Any]] = []
    for inst in _installations(KIND_PLUGIN):
        components = dict(inst.payload.get("components") or {})
        entries.append(
            {
                "install_id": str(inst.payload.get("cloud_install_id") or inst.install_id),
                "slug": inst.key,
                "name": inst.display_name or inst.key,
                "version": inst.version or "",
                "description": inst.description or "",
                "category": str(inst.payload.get("category") or ""),
                "icon": None,
                "source": "",
                "enabled": bool(inst.enabled),
                # 本机可用性：文件已就绪才谈得上调用，与用户的开关无关。
                "callable": bool(inst.ready),
                "is_global": False,
                "skills": list(components.get("skills") or []),
                "mcp": list(components.get("mcp") or []),
                "tools": [],
                "import_report": {},
                "created_at": None,
            }
        )
    return entries


def plugin_ui_contributions() -> List[Dict[str, Any]]:
    """云端投影插件的界面贡献（只给本机启用着的那些）。

    界面贡献必须和启停同源：启停写在本机登记表，所以这份声明也从本机存的清单读。
    回头去问云端的话，用户在本机关掉的插件、面板还会留在界面上。
    """
    if not active():
        return []
    from core.services.plugin_ui_contract import public_contributions

    from . import plugins as caps_plugins, store

    out: List[Dict[str, Any]] = []
    for inst in _installations(KIND_PLUGIN):
        if not (inst.enabled and inst.ready):
            continue
        comp = store.get(KIND_PLUGIN, inst.profile_id, inst.key, inst.resolved_revision)
        if comp is None:
            continue
        try:
            manifest = caps_plugins.load_manifest(comp)
        except (OSError, ValueError):
            # 清单读不出来只影响这一个插件的界面，不该让整页拿不到贡献。
            continue
        public = public_contributions(manifest.get("ui_contributions"), slug=inst.key)
        if public.get("contributes"):
            out.append(public)
    return out


def _projected_plugin(install_id_or_slug: str):
    """按云端 install_id、slug 或本机 install_id 定位一条投影插件。"""
    for inst in _installations(KIND_PLUGIN):
        cloud_id = str(inst.payload.get("cloud_install_id") or "")
        if install_id_or_slug in (inst.key, cloud_id, inst.install_id):
            return inst
    return None


def _component_skill(profile: str, skill_id: str) -> Optional[Dict[str, Any]]:
    from core.config.catalog_loader import skill_body_from_raw

    from . import registry

    inst = registry.get(registry.install_id(KIND_SKILL, profile, skill_id))
    if inst is None or inst.state == "removed":
        return None
    body = _skill_body(inst)
    return {
        "skill_id": skill_id,
        "name": inst.display_name or skill_id,
        "description": inst.description or "",
        "version": inst.version or "",
        "tags": [],
        "enabled": bool(inst.enabled),
        "instructions": skill_body_from_raw(body[0]) if body else "",
        "files": list(body[1]) if body else [],
        "has_secrets": False,
    }


def _component_connector(server_id: str, known: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    entry = known.get(server_id, {})
    return {
        "server_id": server_id,
        "name": str(entry.get("display_name") or server_id),
        "description": str(entry.get("description") or ""),
        # 云端下发的连接器一律经云端网关调用，本机不起进程。
        "transport": "cloud_gateway",
        "url": None,
        "enabled": bool(entry.get("enabled", True)),
        "needs_runtime": False,
        "tools": list(entry.get("tools") or []),
    }


def plugin_detail(install_id_or_slug: str) -> Optional[Dict[str, Any]]:
    """云端投影插件的详情：它的技能与连接器都从登记表取，界面才不会是个空壳。"""
    if not active():
        return None
    inst = _projected_plugin(install_id_or_slug)
    if inst is None:
        return None
    profile = inst.profile_id
    components = dict(inst.payload.get("components") or {})
    skills = [
        item
        for item in (_component_skill(profile, str(sid)) for sid in components.get("skills") or [])
        if item is not None
    ]
    known = {c["server_id"]: c for c in _managed_connectors()}
    return {
        "install_id": str(inst.payload.get("cloud_install_id") or inst.install_id),
        "slug": inst.key,
        "name": inst.display_name or inst.key,
        "is_global": False,
        "version": inst.version or "",
        "description": inst.description or "",
        "category": str(inst.payload.get("category") or ""),
        "icon": None,
        "source": "",
        "import_report": {},
        "skills": skills,
        "mcp": [_component_connector(str(mid), known) for mid in components.get("mcp") or []],
        "admin_config": None,
        "connection": None,
    }


# ── 启停 ──────────────────────────────────────────────────────────────


def set_enabled(kind: str, item_id: str, enabled: bool) -> bool:
    """把启停写进登记表；返回 False 表示这条不是云端投影项，调用方照常处理。"""
    if not active():
        return False
    from . import registry

    profile = _profile()
    if not profile:
        return False
    inst = registry.get(registry.install_id(kind, profile, item_id))
    if inst is None or inst.state == "removed":
        return False
    registry.set_enabled(inst.install_id, bool(enabled))
    _invalidate()
    return True


def set_plugin_enabled(install_id_or_slug: str, enabled: bool) -> bool:
    """插件按云端 install_id 或 slug 定位后写登记表。"""
    if not active():
        return False
    from . import registry

    inst = _projected_plugin(install_id_or_slug)
    if inst is None:
        return False
    registry.set_enabled(inst.install_id, bool(enabled))
    _invalidate()
    return True


def _invalidate() -> None:
    """启停变了，运行时解析与目录缓存都要重算。"""
    try:
        from core.agent_skills.cache_refresh import refresh_skill_caches
        from core.config.catalog_runtime import invalidate_runtime_catalog_cache

        invalidate_runtime_catalog_cache()
        refresh_skill_caches()
    except Exception as exc:  # noqa: BLE001 - 缓存刷新失败不该让启停本身失败
        logger.warning("[device-catalog] cache refresh after toggle failed: %s", exc)


__all__ = [
    "active",
    "agent_entries",
    "catalog_overlay",
    "merge_items",
    "plugin_detail",
    "plugin_entries",
    "set_enabled",
    "set_plugin_enabled",
    "toggle_projected_agent",
]
