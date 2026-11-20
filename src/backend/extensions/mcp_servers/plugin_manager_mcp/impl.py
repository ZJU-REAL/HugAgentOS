"""Cloud marketplace operations for plugin-manager; installation lifecycle lives in cloud_management."""

from __future__ import annotations
from features.marketplace import plugin_market

import logging
from typing import Any, Dict, Optional

from extensions.mcp_servers._manager_common import invalidate_user_cache, no_user, require_cap

logger = logging.getLogger(__name__)

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

def search_plugin_market(*, user_id: str, query: str = "", category: str = "") -> Dict[str, Any]:
    if not user_id:
        return no_user()
    from core.db.engine import SessionLocal

    q = (query or "").strip().lower()
    cat = (category or "").strip()
    with SessionLocal() as db:
        # include_disabled=False：用户侧只看得到已发布的条目，与前端插件市场同口径。
        items = plugin_market.list_plugins(db, user_id, include_disabled=False) or []

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

def get_plugin_info(*, user_id: str, slug: str) -> Dict[str, Any]:
    if not user_id:
        return no_user()
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "message": "❌ 请提供插件 slug。"}
    from core.db.engine import SessionLocal
    from core.capabilities.plugins import management as plugin_service

    with SessionLocal() as db:
        # 可见范围守卫：对该用户不可见的 scoped 条目按不存在处理（与用户端详情接口一致）。
        from features.marketplace import edition as ml

        if not ml.is_item_visible(db, ml.KIND_PLUGIN, slug, user_id):
            return {"ok": False, "message": f"❌ 插件市场里找不到「{slug}」。"}
        detail = None
        try:
            detail = plugin_service.get_plugin_detail(slug, db)
        except Exception as exc:
            return {"ok": False, "message": f"❌ 查不到市场插件「{slug}」：{exc}。已安装内容请用 get_plugin(install_id)。"}
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

def install_plugin(
    *, user_id: str, slug: str, secrets: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    if not user_id:
        return no_user()
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "message": "❌ 请提供要安装的插件 slug。"}
    from core.db.engine import SessionLocal
    from core.capabilities.plugins import management as plugin_service

    with SessionLocal() as db:
        cap_err = require_cap(db, user_id, "can_install_market_plugin", source="plugin_manager")
        if cap_err:
            return cap_err
        # 可见范围守卫：与用户端安装接口一致；不要包 try——守卫出错就该报错，不能对不可见条目失败开放。
        from features.marketplace import edition as ml

        if not ml.is_item_visible(db, ml.KIND_PLUGIN, slug, user_id):
            return {"ok": False, "message": f"❌ 插件市场里找不到「{slug}」。"}
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
    invalidate_user_cache(user_id, source="plugin_manager")
    report = (res or {}).get("import_report") or {}
    return {
        "ok": True,
        "install_id": (res or {}).get("install_id"),
        "import_report": report,
        "message": f"✅ 已安装插件「{slug}」到你的空间，里面的技能和工具现在就能用。",
    }
