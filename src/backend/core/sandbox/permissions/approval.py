"""Account approval preferences and dispatch-scoped runtime resolution."""

from __future__ import annotations
import asyncio
import logging
from dataclasses import replace
from typing import Any, Optional

logger = logging.getLogger(__name__)

APPROVAL_ASK = "ask"
APPROVAL_AUTO = "auto"
APPROVAL_FULL = "full"
APPROVAL_MODES = (APPROVAL_ASK, APPROVAL_AUTO, APPROVAL_FULL)

# 「替我批准」档下仍要停下来问的操作：删掉的东西找不回来，值得多按一次。
DESTRUCTIVE_OPS = frozenset({"delete", "cron_delete"})


def normalize_approval_mode(raw: Any) -> str:
    """Coerce a stored or user-supplied preset name onto the known vocabulary."""
    mode = str(raw or "").strip().lower()
    if mode in APPROVAL_MODES:
        return mode
    return APPROVAL_ASK


def resolve_approval_mode(explicit: Any, *, user_id: Optional[str]) -> str:
    """本次运行的权限档：调用方显式给了就用它，否则回落到用户自己存的那一档。

    权限档是**每个用户一份**的设置，任何一个新起 agent 的入口（子智能体、
    计划步骤、后台作业）都该拿到同一份。靠每个调用点各自透传，漏一个就等于
    悄悄退回「逐项确认」——用户明明选了「完全放开」，换条路径照样被问。
    """
    if explicit is not None:
        return normalize_approval_mode(explicit)
    if not user_id:
        return APPROVAL_ASK
    try:
        from core.db.engine import SessionLocal
        from core.auth.users import UserService

        with SessionLocal() as db:
            stored = UserService(db).get_user_settings(str(user_id)).get("tool_approval_mode")
    except Exception:  # noqa: BLE001 - 读不到就按最保守的一档
        logger.warning("[tool-permission] 权限档读取失败，本次按逐项确认处理", exc_info=True)
        return APPROVAL_ASK
    return normalize_approval_mode(stored)


def _preset_answers(mode: str, *, dangerous: bool) -> bool:
    if mode == APPROVAL_FULL:
        return True
    return mode == APPROVAL_AUTO and not dangerous


def preset_answers_for_user(
    user_id: Optional[str], *, op: str = "", dangerous: bool = False
) -> bool:
    """执行期复查：这个用户的权限档是不是已经替他答了这次确认。

    供分派中间件之外的确认调用使用。按账号读取同一权限档，
    不依赖当前异步任务的上下文。
    """
    return _preset_answers(
        resolve_approval_mode(None, user_id=user_id),
        dangerous=dangerous or op in DESTRUCTIVE_OPS,
    )


async def refresh_runtime(runtime):
    """Resolve one immutable dispatch policy; a failed read is an error, never a prompt."""
    if not runtime.account_approval or not runtime.user_id or runtime.default_allow:
        return runtime
    from core.sandbox.permissions.account import read_account_mode
    from core.sandbox.permissions.dispatch_models import PermissionEnforcementError

    try:
        mode = await asyncio.to_thread(read_account_mode, runtime.user_id, runtime.chat_id)
    except Exception as exc:
        raise PermissionEnforcementError("暂时无法读取执行权限，请稍后重试") from exc
    if mode is None:
        raise PermissionEnforcementError("当前会话的权限归属已失效")
    return replace(runtime, approval_mode=mode)
