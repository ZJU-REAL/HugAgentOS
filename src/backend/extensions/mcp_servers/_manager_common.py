"""Helpers shared by the agent / skill / plugin manager MCP implementations."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def no_user() -> Dict[str, Any]:
    return {"ok": False, "message": "❌ 无法确定用户身份（缺 X-Current-User-Id 头），拒绝操作。"}


def invalidate_user_cache(user_id: Optional[str], *, source: str) -> None:
    """清掉该用户的 30s 能力解析缓存（owner 为 None 时清全部）。

    MCP 跑在独立进程，能力缓存是**进程内**的：这里只清得掉本进程的，清不到 backend 进程
    （智能体真正读缓存的地方）。对智能体侧的有效失效由前端在变更类工具返回后重拉
    catalog / agents / plugins 完成，映射见 ``src/frontend/src/utils/toolRefresh.ts`` 的
    ``MUTATING_TOOL_REFRESH``。本调用作为进程内一致性的兜底保留，无害。
    """
    try:
        from core.capabilities.catalog.resolver import invalidate_capability_cache

        invalidate_capability_cache(str(user_id) if user_id else None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: invalidate_capability_cache failed (%s)", source, exc)


def require_cap(db, user_id: str, cap: str, *, source: str) -> Optional[Dict[str, Any]]:
    """能力位校验。缺权限返回错误 dict，否则 None。"""
    try:
        from core.auth.capabilities import resolve_user_capabilities

        if not resolve_user_capabilities(db, user_id).get(cap):
            return {
                "ok": False,
                "message": f"❌ 管理员未开放该能力（{cap}），无法执行。请联系管理员在权限设置里开启。",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: capability check failed (%s)", source, exc)
        return {"ok": False, "message": f"❌ 权限校验失败：{exc}"}
    return None
