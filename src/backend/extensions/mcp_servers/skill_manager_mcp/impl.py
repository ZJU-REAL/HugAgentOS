"""Cloud marketplace operations for skill-manager; installation lifecycle lives in cloud_management."""

from __future__ import annotations
from features.marketplace import skill_catalog
from features.marketplace import skill_installation
from features.marketplace import skill_submissions

import logging
from typing import Any, Dict, Optional

from extensions.mcp_servers._manager_common import invalidate_user_cache, no_user, require_cap

logger = logging.getLogger(__name__)

def search_marketplace(*, user_id: str, query: str = "", category: str = "") -> Dict[str, Any]:
    if not user_id:
        return no_user()
    from core.db.engine import SessionLocal


    q = (query or "").strip().lower()
    cat = (category or "").strip()
    with SessionLocal() as db:
        # 与用户端市场接口同一套可见范围过滤（scoped 条目仅授权者可见）
        items = skill_catalog.list_marketplace_skills(db, viewer_user_id=user_id)

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
        return no_user()
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "message": "❌ 请提供要安装的技能 slug。"}
    from core.db.engine import SessionLocal


    with SessionLocal() as db:
        cap_err = require_cap(db, user_id, "can_install_market_skill", source="skill_manager")
        if cap_err:
            return cap_err
        # 可见范围守卫：对该用户不可见的 scoped 条目按不存在处理（与用户端安装接口一致）
        from features.marketplace.edition import is_item_visible
        from features.marketplace import edition as ml
        if not is_item_visible(db, ml.KIND_SKILL, slug, user_id):
            return {"ok": False, "message": f"❌ 技能市场里找不到「{slug}」。"}
        try:
            res = skill_installation.install_marketplace_skill(
                db, slug, owner_user_id=user_id, secrets=secrets or {}
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 安装失败：{exc}"}
    invalidate_user_cache(user_id, source="skill_manager")
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
        return no_user()
    skill_id = (skill_id or "").strip()
    if not skill_id:
        return {"ok": False, "message": "❌ 请提供要上架的私有技能 skill_id（先用 list_skills 查看）。"}
    from core.db.engine import SessionLocal


    with SessionLocal() as db:
        cap_err = require_cap(db, user_id, "can_add_skill", source="skill_manager")
        if cap_err:
            return cap_err
        try:
            res = skill_submissions.submit_to_marketplace(
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
