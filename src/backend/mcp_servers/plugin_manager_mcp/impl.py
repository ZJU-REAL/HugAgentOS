"""插件管理 MCP —— 业务实现（直连 DB / 复用 plugin_service，按 X-Current-User-Id 归属）。

复用 ``plugin_service``（市场列表/详情/安装/导入/启停/卸载）与 ``mcp_servers._packaging``
（共享产物库读取 + 安全解包）。所有写操作强制按 ``user_id`` 归属，落成该用户的私有安装，
绝不写全局（``owner_user_id=None`` 是管理员通道，自助入口不得触碰）。

自锁守卫：本插件自己也在可管理范围内，卸载它、或仅仅停用它，都会让这些工具从能力目录里
消失，之后再没有工具能把它恢复（用户只能去后台）。守的是这个不变量而非某个动词，
故 ``uninstall_plugin`` 与 ``set_plugin_enabled(enabled=False)`` 都过 ``_guard_self_lockout``。

启停一律走 ``*_for_user`` 用户级通路（写每用户覆写），不碰组件的全局 ``is_enabled``——
后者是管理员通道，且启用态解析"覆写优先"，改错层会出现"提示已停用、列表仍显示启用"。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 本插件自身的 slug —— 自卸载守卫用。
SELF_SLUG = "plugin-manager"


# ── 通用 ────────────────────────────────────────────────────────────────
def _no_user() -> Dict[str, Any]:
    return {"ok": False, "message": "❌ 无法确定用户身份（缺 X-Current-User-Id 头），拒绝操作。"}


def _invalidate_user_cache(user_id: Optional[str]) -> None:
    """清掉该用户的 30s 能力解析缓存（仅本进程；跨进程失效靠前端重拉 /v1/catalog）。

    见 agent_manager_mcp/impl.py 同名函数的说明——两个 MCP 面对的是同一个跨进程缓存问题。
    """
    try:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache(str(user_id) if user_id else None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("plugin_manager: invalidate_capability_cache failed (%s)", exc)


def _require_cap(db, user_id: str, cap: str) -> Optional[Dict[str, Any]]:
    """能力位校验。缺权限返回错误 dict，否则 None。"""
    try:
        from core.auth.capabilities import resolve_user_capabilities

        if not resolve_user_capabilities(db, user_id).get(cap):
            return {
                "ok": False,
                "message": f"❌ 管理员未开放该能力（{cap}），无法执行。请联系管理员在权限设置里开启。",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin_manager: capability check failed (%s)", exc)
        return {"ok": False, "message": f"❌ 权限校验失败：{exc}"}
    return None


def _guard_self_lockout(hit: Dict[str, Any], verb: str) -> Optional[Dict[str, Any]]:
    """拦住"把自己关掉"。

    守的是不变量而不是某个动词：卸载本插件和停用本插件（乃至只停用它的 MCP 组件）
    通向同一个死局——这些工具本身随之从能力目录里消失，就再没有工具能把它开回来。
    重新启用自己是无害的，只在 enabled=False / 卸载时调用。
    """
    if str(hit.get("slug") or "") != SELF_SLUG:
        return None
    return {
        "ok": False,
        "message": (
            f"❌ 不能{verb}「插件管理」插件本身——{verb}之后它的工具会从你的能力里消失，"
            "就没有工具能把它恢复了。如果确实要这么做，请让用户到管理后台的「插件管理」里操作。"
        ),
    }


def _is_installed(db, slug: str, user_id: str) -> bool:
    """该 slug 是否已装（本人私有安装或管理员全局安装）。

    按列直查而不是拼 install_id：``slug@owner`` 是 plugin_service 的内部约定，
    在这里复刻一份就等于把它变成公开契约。
    """
    from core.db.models import InstalledPlugin
    from sqlalchemy import or_

    return (
        db.query(InstalledPlugin.install_id)
        .filter(
            InstalledPlugin.slug == slug,
            or_(
                InstalledPlugin.owner_user_id == user_id,
                InstalledPlugin.owner_user_id.is_(None),
            ),
        )
        .first()
        is not None
    )


def _slim_installed(it: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "install_id": it.get("install_id"),
        "slug": it.get("slug"),
        "name": it.get("name"),
        "description": (str(it.get("description") or ""))[:160],
        "category": it.get("category") or "",
        "enabled": bool(it.get("enabled")),
        "is_global": bool(it.get("is_global")),
        "components": {
            "skills": len(it.get("skills") or []),
            "mcp": len(it.get("mcp") or []),
        },
    }


def _resolve_installed(
    db, user_id: str, ref: str
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """ref → (唯一命中, 候选)。先精确 install_id，再精确 slug，最后按名称模糊匹配。

    只在"我自己装的私有插件"里找：管理员下发的全局插件（is_global）用户改不了，
    在这里当作不存在，免得智能体拿着一个注定失败的 install_id 反复重试。
    """
    from core.services import plugin_service

    ref = (ref or "").strip()
    rows = plugin_service.list_installed(db, user_id, include_global=False) or []
    for it in rows:
        if str(it.get("install_id")) == ref:
            return it, []
    exact_slug = [it for it in rows if str(it.get("slug")) == ref]
    if len(exact_slug) == 1:
        return exact_slug[0], []
    if len(exact_slug) > 1:
        return None, exact_slug[:10]
    low = ref.lower()
    cands = [it for it in rows if low and low in str(it.get("name") or "").lower()]
    if len(cands) == 1:
        return cands[0], []
    return None, cands[:10]


def _need_clarify(cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "ok": False,
        "need_clarification": True,
        "message": "匹配到多个插件，请用 install_id 指明具体是哪一个：",
        "candidates": [
            {"install_id": c.get("install_id"), "slug": c.get("slug"), "name": c.get("name")}
            for c in cands
        ],
    }


# ── ① 搜索插件市场（只读）──────────────────────────────────────────────
def search_plugin_market(*, user_id: str, query: str = "", category: str = "") -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    from core.db.engine import SessionLocal
    from core.services import plugin_service

    q = (query or "").strip().lower()
    cat = (category or "").strip()
    with SessionLocal() as db:
        # include_disabled=False：用户侧只看得到已发布的条目，与前端插件市场同口径。
        items = plugin_service.list_plugins(db, user_id, include_disabled=False) or []

    def _match(it: Dict[str, Any]) -> bool:
        if cat and str(it.get("category") or "") != cat:
            return False
        if not q:
            return True
        hay = " ".join(
            str(it.get(k) or "")
            for k in ("slug", "name", "display_name", "description", "category")
        )
        return q in hay.lower()

    hits = [it for it in items if _match(it)]
    # 展示名由 _overlay_market_meta 直接覆盖进 item["name"]，没有单独的 display_name 键。
    slim = [
        {
            "slug": it.get("slug"),
            "name": it.get("name") or it.get("slug"),
            "description": (str(it.get("description") or ""))[:200],
            "category": it.get("category") or "",
            "installed": bool(it.get("installed")),
        }
        for it in hits
    ]
    msg = (
        f"插件市场匹配 {len(slim)} 个"
        + (f"（关键词「{query}」）" if q else "")
        + (f"（分类「{cat}」）" if cat else "")
        + "。建议先用 get_plugin_info(slug) 看看它会带进来什么，再决定装不装。"
    )
    return {"ok": True, "count": len(slim), "plugins": slim, "message": msg}


# ── ② 插件详情（只读，安装前必看）──────────────────────────────────────
def get_plugin_info(*, user_id: str, slug: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "message": "❌ 请提供插件 slug。"}
    from core.db.engine import SessionLocal
    from core.services import plugin_service

    with SessionLocal() as db:
        detail = None
        try:
            detail = plugin_service.get_plugin_detail(slug, db)
        except Exception as market_exc:  # noqa: BLE001
            # 市场里没有 ≠ 查不到：用户自己 import 进来的插件不在市场目录里，
            # 但它确实装着、能停用能卸载。这时回落到"已装详情"，
            # 否则用户刚导入完一问详情就得到"查不到插件"，只能自己猜发生了什么。
            hit, _cands = _resolve_installed(db, user_id, slug)
            if hit is None:
                return {"ok": False, "message": f"❌ 查不到插件「{slug}」：{market_exc}"}
            try:
                detail = plugin_service.get_installed_detail(
                    db, str(hit.get("install_id")), owner_user_id=user_id
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"❌ 查不到插件「{slug}」：{exc}"}
        # detail 本身不带 installed，这里补一次，免得用户重复安装。
        # 走主键直查而不是 list_installed：后者会连带跑一整套启用态解析
        # （resolve_all_runtime_enabled 约 6~7 次查询）再逐行读插件目录的 plugin.json，
        # 结果全被丢掉，只为得到一个布尔值。
        installed = _is_installed(db, slug, user_id)

    skills = detail.get("skills") or []
    mcps = detail.get("mcp") or []
    secrets = list(detail.get("required_secrets") or [])
    name = detail.get("name") or slug
    return {
        "ok": True,
        "slug": detail.get("slug") or slug,
        "name": name,
        "description": detail.get("description") or "",
        "category": detail.get("category") or "",
        "installed": installed,
        "skills": [
            {
                "name": s.get("name") or s.get("skill_id"),
                "description": (str(s.get("description") or ""))[:160],
            }
            for s in skills
            if isinstance(s, dict)
        ],
        "mcp_servers": [
            {
                "name": m.get("name") or m.get("server_id"),
                "description": (str(m.get("description") or ""))[:160],
                "tools": list(m.get("tools") or []),
            }
            for m in mcps
            if isinstance(m, dict)
        ],
        "required_secrets": secrets,
        "message": (
            f"插件「{name}」会带进来 {len(skills)} 个技能、{len(mcps)} 个工具"
            + (f"，需要先准备这些凭据：{'、'.join(str(s) for s in secrets)}" if secrets else "")
            + ("。你已经装过它了。" if installed else "。把这些讲给用户听，再问要不要装。")
        ),
    }


# ── ③ 从市场安装（私有，写）────────────────────────────────────────────
def install_plugin(
    *, user_id: str, slug: str, secrets: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "message": "❌ 请提供要安装的插件 slug。"}
    from core.db.engine import SessionLocal
    from core.services import plugin_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_import_plugin")
        if cap_err:
            return cap_err
        try:
            res = plugin_service.install_plugin(
                db,
                slug,
                owner_user_id=user_id,
                secrets=secrets or {},
                created_by="agent_plugin_manager",
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 安装失败：{exc}"}
    _invalidate_user_cache(user_id)
    report = (res or {}).get("import_report") or {}
    return {
        "ok": True,
        "install_id": (res or {}).get("install_id"),
        "import_report": report,
        "message": f"✅ 已安装插件「{slug}」到你的空间，里面的技能和工具现在就能用。",
    }


# ── ④ 我装了哪些（只读）────────────────────────────────────────────────
def list_my_plugins(*, user_id: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    from core.db.engine import SessionLocal
    from core.services import plugin_service

    with SessionLocal() as db:
        # include_global=True：把管理员下发的全局插件也列出来（用户看得见但改不了），
        # 否则用户问"我有哪些插件"时会漏掉一半，进而反复要求安装已经有的东西。
        rows = plugin_service.list_installed(db, user_id, include_global=True) or []
    plugins = [_slim_installed(it) for it in rows]
    mine = [p for p in plugins if not p["is_global"]]
    msg = (
        f"你有 {len(plugins)} 个插件（其中 {len(mine)} 个是你自己装的，可停用/卸载；"
        f"另外 {len(plugins) - len(mine)} 个是管理员下发的全局插件，只能用不能改）。"
        if plugins
        else "你还没有装任何插件（可用 search_plugin_market 找找）。"
    )
    return {"ok": True, "count": len(plugins), "plugins": plugins, "message": msg}


# ── ⑤ 从产物库导入插件包（写）──────────────────────────────────────────
def import_plugin(*, user_id: str, artifact_id: str) -> Dict[str, Any]:
    """把沙箱产出/网上下载的插件包落库成我的私有插件。

    通路与 skill-manager 的 register_skill 一致：沙箱里备好插件目录 → tar/zip 打包 →
    sandbox_get_artifact 取得 artifact_id → 本工具从共享产物库读出、安全解包、导入。
    """
    if not user_id:
        return _no_user()
    artifact_id = (artifact_id or "").strip()
    if not artifact_id:
        return {
            "ok": False,
            "message": "❌ 缺少 artifact_id。请先在沙箱里把插件目录打成 tar，再调 sandbox_get_artifact 取得 artifact_id。",
        }

    from core.services import plugin_service
    from mcp_servers._packaging import is_plugin_root, locate_root, read_artifact_bytes, safe_extract

    data = read_artifact_bytes(artifact_id)
    if data is None:
        return {"ok": False, "message": f"❌ 产物库里找不到 artifact_id「{artifact_id}」（或已过期）。"}

    from core.db.engine import SessionLocal

    with tempfile.TemporaryDirectory(prefix="plugimp_") as tmp:
        tmp_path = Path(tmp)
        try:
            safe_extract(data, tmp_path)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 解包失败：{exc}"}

        root = locate_root(tmp_path)
        if root is None or not is_plugin_root(root):
            return {
                "ok": False,
                "message": (
                    "❌ 这个包里没有 plugin.json，不是一个插件包。"
                    "如果你想装的是单个技能，请改用技能管理插件的 register_skill。"
                    "插件包该长什么样见 plugin-creator 技能。"
                ),
            }

        with SessionLocal() as db:
            cap_err = _require_cap(db, user_id, "can_import_plugin")
            if cap_err:
                return cap_err
            try:
                res = plugin_service.import_plugin(
                    db, root, owner_user_id=user_id, created_by="agent_plugin_manager"
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"❌ 插件导入失败：{exc}"}
    _invalidate_user_cache(user_id)
    report = (res or {}).get("import_report") or {}
    return {
        "ok": True,
        "install_id": (res or {}).get("install_id"),
        "import_report": report,
        "message": "✅ 已作为插件导入到你的私有空间，里面的技能和工具现在就能用。",
    }


# ── ⑥ 启停（写）────────────────────────────────────────────────────────
def set_plugin_enabled(
    *,
    user_id: str,
    plugin_ref: str,
    enabled: bool,
    component_kind: str = "",
    component_id: str = "",
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    plugin_ref = (plugin_ref or "").strip()
    if not plugin_ref:
        return {"ok": False, "message": "❌ 请提供插件的 install_id 或名称。"}
    kind = (component_kind or "").strip().lower()
    comp = (component_id or "").strip()
    if kind and kind not in ("skill", "mcp"):
        return {"ok": False, "message": "❌ component_kind 只能是 skill 或 mcp。"}
    if bool(kind) != bool(comp):
        return {"ok": False, "message": "❌ component_kind 和 component_id 必须同时提供（只关插件里的某个组件时用）。"}

    from core.db.engine import SessionLocal
    from core.services import plugin_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_import_plugin")
        if cap_err:
            return cap_err
        hit, cands = _resolve_installed(db, user_id, plugin_ref)
        if hit is None and cands:
            return _need_clarify(cands)
        if hit is None:
            return {
                "ok": False,
                "message": f"❌ 没找到你自己安装的插件「{plugin_ref}」（管理员下发的全局插件不能在这里改）。",
            }
        if not enabled:
            lock = _guard_self_lockout(hit, "停用")
            if lock:
                return lock

        install_id = str(hit.get("install_id"))
        try:
            # 走用户级通路（写每用户覆写），不是管理员级的全局 is_enabled：
            # 启用态解析是"覆写优先"，若用户曾在插件页点过开关就会留下覆写，
            # 改全局位会被覆写盖掉——工具回了"已停用"，list_my_plugins 却仍显示启用。
            if comp:
                plugin_service.set_plugin_component_enabled_for_user(
                    db, install_id, kind=kind, component_id=comp, enabled=bool(enabled), user_id=user_id
                )
            else:
                plugin_service.set_plugin_enabled_for_user(
                    db, install_id, enabled=bool(enabled), user_id=user_id
                )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 操作失败：{exc}"}
    _invalidate_user_cache(user_id)
    word = "启用" if enabled else "停用"
    target = f"插件「{hit.get('name')}」里的 {kind} 组件「{comp}」" if comp else f"插件「{hit.get('name')}」"
    return {
        "ok": True,
        "install_id": install_id,
        "enabled": bool(enabled),
        "message": f"✅ 已{word}{target}。数据都留着，随时可以再改回来。",
    }


# ── ⑦ 卸载（写，破坏性）────────────────────────────────────────────────
def uninstall_plugin(*, user_id: str, plugin_ref: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    plugin_ref = (plugin_ref or "").strip()
    if not plugin_ref:
        return {"ok": False, "message": "❌ 请提供要卸载的插件 install_id 或名称。"}
    from core.db.engine import SessionLocal
    from core.services import plugin_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_import_plugin")
        if cap_err:
            return cap_err
        hit, cands = _resolve_installed(db, user_id, plugin_ref)
        if hit is None and cands:
            return _need_clarify(cands)
        if hit is None:
            return {
                "ok": False,
                "message": f"❌ 没找到你自己安装的插件「{plugin_ref}」（管理员下发的全局插件不能在这里卸载）。",
            }

        lock = _guard_self_lockout(hit, "卸载")
        if lock:
            return lock

        install_id = str(hit.get("install_id"))
        display = hit.get("name")
        try:
            res = plugin_service.uninstall_plugin(db, install_id, owner_user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 卸载失败：{exc}"}
    _invalidate_user_cache(user_id)
    removed = res or {}
    return {
        "ok": True,
        "install_id": install_id,
        "removed": removed,
        "message": (
            f"✅ 已卸载插件「{display}」，它带进来的技能和工具已一并删除。"
            "如果只是暂时不想用，下次可以用 set_plugin_enabled 停用，不必卸载。"
        ),
    }
