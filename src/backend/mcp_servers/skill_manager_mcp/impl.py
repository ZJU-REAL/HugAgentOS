"""Cloud marketplace operations for skill-manager; installation lifecycle lives in cloud_management."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

def _no_user() -> Dict[str, Any]:
    return {"ok": False, "message": "❌ 无法确定用户身份（缺 X-Current-User-Id 头），拒绝操作。"}

def _invalidate_user_cache(user_id: Optional[str]) -> None:
    """清掉该用户的 30s 能力解析缓存。owner 为 None（全局技能）时清全部。

    注意：本 MCP 跑在独立的 ``mcp`` 容器进程，能力缓存是**进程内**的——这里只清得掉本进程的，
    清不到 backend 进程（智能体真正读缓存的地方）。对智能体侧的**有效**失效由前端在技能变更后
    重新拉 ``GET /v1/catalog``（跑在 backend 进程）顺带完成，见 api/routes/v1/catalog.py。
    本调用作为进程内一致性的兜底保留，无害。"""
    try:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache(str(user_id) if user_id else None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("skill_manager: invalidate_capability_cache failed (%s)", exc)

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
        logger.warning("skill_manager: capability check failed (%s)", exc)
        return {"ok": False, "message": f"❌ 权限校验失败：{exc}"}
    return None

def search_marketplace(*, user_id: str, query: str = "", category: str = "") -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    from core.db.engine import SessionLocal
    from core.services import marketplace_service

    q = (query or "").strip().lower()
    cat = (category or "").strip()
    with SessionLocal() as db:
        # 与用户端市场接口同一套可见范围过滤（scoped 条目仅授权者可见）
        items = marketplace_service.list_marketplace_skills(db, viewer_user_id=user_id)

    def _match(it: Dict[str, Any]) -> bool:
        if cat and str(it.get("category") or "") != cat:
            return False
        if not q:
            return True
        hay = " ".join(
            str(it.get(k) or "")
            for k in ("slug", "display_name", "summary", "description", "category")
        )
        hay += " " + " ".join(str(t) for t in (it.get("tags") or []))
        return q in hay.lower()

    hits = [it for it in items if _match(it)]
    slim = [
        {
            "slug": it.get("slug"),
            "display_name": it.get("display_name"),
            "summary": it.get("summary") or it.get("description") or "",
            "category": it.get("category"),
            "tags": it.get("tags") or [],
            "installed": bool(it.get("installed")),
            "source": it.get("source"),
        }
        for it in hits
    ]
    msg = (
        f"技能市场匹配 {len(slim)} 个技能"
        + (f"（关键词「{query}」）" if q else "")
        + (f"（分类「{cat}」）" if cat else "")
        + "。想安装某个用 install_from_marketplace(slug)。"
    )
    return {"ok": True, "count": len(slim), "skills": slim, "message": msg}

def install_from_marketplace(
    *, user_id: str, slug: str, secrets: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "message": "❌ 请提供要安装的技能 slug。"}
    from core.db.engine import SessionLocal
    from core.services import marketplace_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_skill")
        if cap_err:
            return cap_err
        # 可见范围守卫：对该用户不可见的 scoped 条目按不存在处理（与用户端安装接口一致）
        from core.auth.marketplace_visibility import is_item_visible
        from core.services import marketplace_listing as ml
        if not is_item_visible(db, ml.KIND_SKILL, slug, user_id):
            return {"ok": False, "message": f"❌ 技能市场里找不到「{slug}」。"}
        try:
            res = marketplace_service.install_marketplace_skill(
                db, slug, owner_user_id=user_id, secrets=secrets or {}
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 安装失败：{exc}"}
    _invalidate_user_cache(user_id)
    return {
        "ok": True,
        "skill_id": res.get("id") or res.get("skill_id"),
        "action": res.get("action"),
        "message": f"✅ 已安装技能「{slug}」到你的私有技能库，可直接在对话中使用。",
    }

def submit_to_marketplace(
    *,
    user_id: str,
    skill_id: str,
    category: str = "",
    summary: str = "",
    note: str = "",
    submitter_name: str = "",
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    skill_id = (skill_id or "").strip()
    if not skill_id:
        return {"ok": False, "message": "❌ 请提供要上架的私有技能 skill_id（先用 list_skills 查看）。"}
    from core.db.engine import SessionLocal
    from core.services import marketplace_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_skill")
        if cap_err:
            return cap_err
        try:
            res = marketplace_service.submit_to_marketplace(
                db,
                skill_id,
                owner_user_id=user_id,
                submitter_name=submitter_name or "智能体代提交",
                note=note,
                category=category,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 上架申请失败：{exc}"}
    return {
        "ok": True,
        "submission_id": res.get("submission_id") or res.get("id"),
        "status": res.get("status", "pending"),
        "message": f"✅ 已提交上架申请（技能 {skill_id}），进入管理员审核队列，审核通过后其他用户即可安装。",
    }
