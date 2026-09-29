"""Internal site KV callback."""

import logging
from typing import Optional

from core.infra.responses import success_response
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .internal_site_auth import _check_internal_token

logger = logging.getLogger(__name__)
router = APIRouter()

# ── 站点 KV（智能体侧读写）────────────────────────────────────────
#
# 站内 JS 走公开的 /site/<slug>/__api/kv/*；沙箱里的智能体没有站点会话 cookie，
# 私密/团队站直接 404，所以它需要一条带身份的通道。这里复用 SiteService 的键名
# 校验、4KB 值上限和 200 键配额，权限级别取自 site_service 的 KV 契约常量——那是
# 「谁能读写 KV」的唯一声明处，三条传输通道都读它，不各自决定。


class KvBody(BaseModel):
    """内部 KV 请求体；面向模型的四个工具在 MCP 侧拆开，这里按 action 归一。"""

    action: str = Field("", description="list | get | set | delete")
    site_id: str = ""
    slug: str = ""
    key: str = ""
    value: str = ""
    limit: int = 50
    user_id: str = ""  # MCP 侧从 X-Current-User-Id 解析


_KV_ACTIONS = frozenset({"list", "get", "set", "delete"})
_KV_WRITE_ACTIONS = frozenset({"set", "delete"})
_KV_PREVIEW_CHARS = 200
_KV_LIST_MAX = 200


def _resolve_site_for_kv(db, body: "KvBody", *, required: str):
    from core.db.repository import SiteRepository
    from core.services.site_service import SiteService

    site_id = (body.site_id or "").strip()
    if not site_id:
        slug = (body.slug or "").strip().strip("/")
        if not slug:
            raise ValueError(
                "需要指定站点：传 site_id（list_sites 查得，或 publish_site 回执里的编号），"
                "或传 slug（站点地址 /site/<slug>/ 中间那段）。"
            )
        found = SiteRepository(db).get_by_slug(slug)
        if not found:
            raise ValueError(f"找不到 slug 为 {slug} 的站点")
        site_id = found.site_id
    return SiteService(db).get_owned(site_id, body.user_id, required=required)


def _run_kv(body: "KvBody", action: str) -> dict:
    from core.db.engine import SessionLocal
    from core.services.site_service import KV_READ_LEVEL, KV_WRITE_LEVEL, SiteService

    with SessionLocal() as db:
        level = KV_WRITE_LEVEL if action in _KV_WRITE_ACTIONS else KV_READ_LEVEL
        site = _resolve_site_for_kv(db, body, required=level)
        service = SiteService(db)
        if action == "list":
            limit = max(1, min(int(body.limit or 50), _KV_LIST_MAX))
            rows, total = service.kv_list(site, limit=limit)
            return {
                "ok": True,
                "site_id": site.site_id,
                "slug": site.slug,
                # total > len(items) 就是「还有更多」的信号；全量回灌会撑爆上下文，
                # 值也只给预览，要全文让模型自己 site_kv_get。
                "total": total,
                "items": [
                    {
                        "key": r.k,
                        "preview": (r.v or "")[:_KV_PREVIEW_CHARS],
                        "value_chars": len(r.v or ""),
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                    for r in rows
                ],
            }
        if action == "get":
            value = service.kv_get(site, body.key)
            return {
                "ok": True,
                "site_id": site.site_id,
                "key": body.key,
                "value": value,
                "exists": value is not None,
            }
        if action == "set":
            service.kv_set(site, body.key, body.value)
            db.commit()
            return {"ok": True, "site_id": site.site_id, "key": body.key}
        service.kv_delete(site, body.key)
        db.commit()
        return {"ok": True, "site_id": site.site_id, "key": body.key, "deleted": True}


@router.post("/kv", summary="读写站点 KV（内部接口）")
async def site_kv(
    body: KvBody,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
):
    _check_internal_token(x_internal_token)
    if not (body.user_id or "").strip():
        return success_response(data={"error": "当前会话缺少用户身份，无法访问站点 KV"})
    # 先认动作再做任何事：桥接分支要拿它拼云端工具名，不能放未校验的串过去。
    action = (body.action or "").strip().lower()
    if action not in _KV_ACTIONS:
        return success_response(data={"error": f"不支持的 KV 操作: {body.action}"})

    from core.services.desktop_cloud_bridge import bridge_enabled

    if bridge_enabled():
        # 混合模式下站点托管在云端，KV 也只存在于云端库——转给云端网关的同名工具。
        from core.services.desktop_site_publish import forward_local_site_tool

        return success_response(
            data=await forward_local_site_tool(
                f"site_kv_{action}",
                body.model_dump(exclude={"user_id", "action"}),
                user_id=body.user_id,
            )
        )

    from core.infra.exceptions import AppException

    try:
        return success_response(data=await run_in_threadpool(_run_kv, body, action))
    except HTTPException as exc:
        return success_response(data={"error": str(exc.detail)})
    except (AppException, ValueError) as exc:
        return success_response(data={"error": getattr(exc, "message", str(exc))})
    except Exception as exc:  # noqa: BLE001 — model-facing, errors must be readable
        logger.exception("internal site kv failed")
        return success_response(data={"error": f"站点 KV 操作失败: {exc}"})
