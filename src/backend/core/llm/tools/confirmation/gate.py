"""Permission gates and human design choices."""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Any, Dict, Optional
from . import state

from .shared import _load_session_grants, _save_session_grant
from .pending import _register_pending, _wait_pending

logger = logging.getLogger(__name__)


async def gate(
    *,
    chat_id: Optional[str],
    op: str,
    logical_path: str,
    interactive: bool,
    summary: str = "",
    timeout: float = state._DEFAULT_WAIT_S,
    kind: str = state.KIND_MYSPACE,
    approval_user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Authorize a write or suspend until its scoped human/account decision."""
    if not state._confirm_enabled(kind):
        return None
    if not interactive:
        if kind == state.KIND_LOCAL_CMD or kind.startswith(state.KIND_LOCAL_PATH_PREFIX):
            return {
                "status": state.STATUS_BLOCKED,
                "op": op,
                "logical_path": logical_path,
                "error": "非交互模式（批量/子智能体）无法向用户确认本机操作，该操作已拒绝。请在主对话中由用户亲自执行或预先调整授权。",
            }
        if (
            kind == state.KIND_AUTOMATION
            or kind == state.KIND_TOOL_PERMISSION
            or kind.startswith(state.KIND_TOOL_PREFIX)
        ):
            return {
                "status": state.STATUS_BLOCKED,
                "op": op,
                "logical_path": logical_path,
                "error": "非交互模式（批量/子智能体/渠道任务）无法向用户确认该工具操作，该操作已拒绝。请在可交互的主对话中由用户亲自执行。",
            }
        return {
            "status": state.STATUS_BLOCKED,
            "op": op,
            "logical_path": logical_path,
            "error": "非交互模式（批量/子智能体）禁止写用户「我的空间」。请改写到本次会话的工作目录（相对路径），或由用户在主对话中亲自操作。",
        }
    cid_key = chat_id or "_nochat_"
    if chat_id:
        for grant_kind, scope, authority in await _load_session_grants(chat_id):
            if (
                authority == (approval_user_id or "")
                and grant_kind == kind
                and state._scope_covers(kind, scope, logical_path)
            ):
                await _save_session_grant(chat_id, grant_kind, scope, authority)
                return None

    def _session_allowed(st: state._ChatConfirm):
        if not chat_id:
            return None
        for granted_kind, granted_scope, authority in st.session_allows:
            if (
                authority == (approval_user_id or "")
                and granted_kind == kind
                and state._scope_covers(kind, granted_scope, logical_path)
            ):
                return (None,)
        return None

    short, cid, p = await _register_pending(
        cid_key,
        (kind, op, logical_path, approval_user_id or ""),
        make_pending=lambda: state._Pending(
            op=op,
            logical_path=logical_path,
            summary=summary or logical_path,
            approval_user_id=approval_user_id,
            ts=time.monotonic(),
            event=asyncio.Event(),
            kind=kind,
        ),
        precheck=_session_allowed,
        timeout=timeout,
    )
    if short is not None:
        return short[0]
    assert cid is not None and p is not None
    state.logger.info(
        "[tool-confirmation] 挂起等待用户确认 chat=%s op=%s path=%s confirm_id=%s",
        cid_key,
        op,
        logical_path,
        cid,
    )

    def _claim_first(pd: state._Pending, decision: str) -> bool:
        first = decision in (state.DECISION_ALLOW, state.DECISION_ALLOW_SESSION) and (
            not pd.performed
        )
        if first:
            pd.performed = True
        return first

    decision, first = await _wait_pending(
        cid_key,
        (kind, op, logical_path, approval_user_id or ""),
        cid,
        p,
        timeout=timeout,
        expire_message="等待确认超时，已自动取消。如仍需要请重新发起。",
        cancel_message="本次运行已取消，该确认已关闭。",
        in_lock_extract=_claim_first,
    )
    if decision in (state.DECISION_ALLOW, state.DECISION_ALLOW_SESSION):
        if first:
            state.logger.info(
                "[tool-confirmation] 用户已批准(%s) chat=%s path=%s → 放行写入",
                decision,
                cid_key,
                logical_path,
            )
            return None
        state.logger.info(
            "[tool-confirmation] 并发重复去重跳过 chat=%s path=%s（首个协程已执行）",
            cid_key,
            logical_path,
        )
        return {
            "status": "deduplicated",
            "ok": True,
            "op": op,
            "logical_path": logical_path,
            "message": state._intercept_message(kind, "dedup", op, logical_path, p.summary),
        }
    if decision == state.DECISION_DENY:
        return {
            "status": "denied_by_user",
            "op": op,
            "logical_path": logical_path,
            "error": state._intercept_message(kind, "deny", op, logical_path, p.summary),
        }
    return {
        "status": "confirm_timeout",
        "op": op,
        "logical_path": logical_path,
        "error": state._intercept_message(kind, "timeout", op, logical_path, p.summary),
    }


async def pick(
    *,
    chat_id: Optional[str],
    question: str,
    options: list,
    interactive: bool,
    timeout: float = state._DEFAULT_WAIT_S,
) -> Dict[str, Any]:
    """Wait for a design choice; permission presets never answer content questions."""
    if not interactive:
        return {
            "status": state.STATUS_BLOCKED,
            "error": "当前为非交互模式（批量/子智能体/渠道机器人），无法弹出设计选择器。请直接选择你认为最合适的方案继续。",
        }
    cid_key = chat_id or "_nochat_"
    key = (state.KIND_DESIGN_PICK, "design_pick", (question or "")[:120])

    def _reject_concurrent(st: state._ChatConfirm):
        for ocid, other in st.pending.items():
            if (
                other.kind == state.KIND_DESIGN_PICK
                and other.decision is None
                and (st.key_index.get(key) != ocid)
            ):
                return (
                    {
                        "status": "already_pending",
                        "error": "已有一个设计方案选择正在等用户操作，禁止并发发起第二个。请等待当前选择结果，按其返回继续。",
                    },
                )
        return None

    short, cid, p = await _register_pending(
        cid_key,
        key,
        make_pending=lambda: state._Pending(
            op="design_pick",
            logical_path=key[2],
            summary=question or "请选择一个设计方案",
            ts=time.monotonic(),
            event=asyncio.Event(),
            kind=state.KIND_DESIGN_PICK,
            payload={"question": question, "options": options},
        ),
        precheck=_reject_concurrent,
        timeout=timeout,
    )
    if short is not None:
        return short[0]
    assert cid is not None and p is not None
    state.logger.info(
        "[design-pick] 挂起等待用户选择 chat=%s confirm_id=%s options=%d",
        cid_key,
        cid,
        len(options),
    )
    decision, choice = await _wait_pending(
        cid_key,
        key,
        cid,
        p,
        timeout=timeout,
        expire_message="设计方案选择已超时，助手将自行选择方案继续。",
        cancel_message="本次运行已取消，设计方案选择已关闭。",
        in_lock_extract=lambda pd, _d: pd.choice,
    )
    if decision == state.DECISION_CHOICE and choice:
        state.logger.info("[design-pick] 用户选中 chat=%s option=%s", cid_key, choice)
        return {"status": "chosen", "option_id": choice}
    if decision == state.DECISION_SKIP:
        return {"status": "skipped"}
    return {"status": "timeout"}
