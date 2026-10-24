"""Local confirmation state, deduplication and session-scoped decisions."""

from __future__ import annotations
from core.sandbox.permissions.vocabulary import (
    KIND_MYSPACE,
    KIND_AUTOMATION,
    KIND_DESIGN_PICK,
    KIND_LOCAL_CMD,
    KIND_LOCAL_PATH_PREFIX,
    KIND_TOOL_PERMISSION,
    KIND_TOOL_PREFIX,
    OP_WRITE,
    OP_EDIT,
    OP_DELETE,
    OP_LOCAL_EXEC,
    OP_LOCAL_READ,
    OP_LOCAL_WRITE,
    OP_MOVE,
    OP_MKDIR,
    OP_CRON_CREATE,
    OP_CRON_UPDATE,
    OP_CRON_DELETE,
    DECISION_ALLOW,
    DECISION_ALLOW_SESSION,
    DECISION_DENY,
    DECISION_CHOICE,
    DECISION_SKIP,
    STATUS_BLOCKED
)

import asyncio
import logging
import os
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from core.llm.runtime.human_interaction import MAX_WAIT_SECONDS

logger = logging.getLogger(__name__)
_DECISIONS = (DECISION_ALLOW, DECISION_ALLOW_SESSION, DECISION_DENY)
_DECISION_TIMEOUT = "_timeout"
_DESIGN_DECISIONS = (DECISION_CHOICE, DECISION_SKIP)
_TTL_S = 1800
_DEFAULT_WAIT_S = MAX_WAIT_SECONDS


@dataclass
class _Pending:
    op: str
    logical_path: str
    summary: str
    ts: float
    event: asyncio.Event
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.get_running_loop)
    decision: Optional[str] = None
    waiters: int = 0
    performed: bool = False
    kind: str = KIND_MYSPACE
    approval_user_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    choice: Optional[str] = None
    registered: bool = False
    registration_error: Optional[BaseException] = None


@dataclass
class _ChatConfirm:
    pending: Dict[str, _Pending] = field(default_factory=dict)
    key_index: Dict[Tuple[str, ...], str] = field(default_factory=dict)
    session_allows: set[Tuple[str, str, str]] = field(default_factory=set)
    ui_signals: Optional[asyncio.Queue] = None
    last_ts: float = field(default_factory=time.monotonic)


def _session_scope(kind: str, logical_path: str) -> str:
    """What a "allow for this whole session" answer actually covers.

    For the host domains the answer is about a place, not about a category:
    approving one write under a project folder should cover that folder for the
    rest of the conversation and nothing else. Approving a command covers that
    program, not every program. The remaining domains have no sub-scope — a
    MySpace approval has always meant "this conversation's MySpace writes".
    """
    if kind.startswith(KIND_LOCAL_PATH_PREFIX):
        resolved = os.path.realpath(os.path.expanduser(logical_path or ""))
        parent = os.path.dirname(resolved)
        return parent or resolved
    if kind == KIND_LOCAL_CMD:
        try:
            parts = shlex.split(logical_path or "")
        except ValueError:
            parts = (logical_path or "").split()
        return parts[0] if parts else ""
    return ""


def _scope_covers(kind: str, granted: str, logical_path: str) -> bool:
    """Whether an existing session grant reaches this target."""
    if kind.startswith(KIND_LOCAL_PATH_PREFIX):
        if not granted:
            return False
        target = os.path.realpath(os.path.expanduser(logical_path or ""))
        return target == granted or target.startswith(granted.rstrip(os.sep) + os.sep)
    if kind == KIND_LOCAL_CMD:
        return bool(granted) and _session_scope(kind, logical_path) == granted
    return True


def _gc(now: float) -> None:
    """Lazily clean up expired chat state (call while holding the lock). Chats with pending items / suspended waiters are not cleaned."""
    dead = [c for c, st in process_state().CHATS.items() if now - st.last_ts > _TTL_S and (not st.pending)]
    for c in dead:
        process_state().CHATS.pop(c, None)


def _get_chat(chat_id: str) -> _ChatConfirm:
    """Get or create per-chat state (call while holding the lock)."""
    st = process_state().CHATS.get(chat_id)
    if st is None:
        st = _ChatConfirm()
        process_state().CHATS[chat_id] = st
    return st


def allow_session(chat_id: Optional[str]) -> None:
    """Pre-authorize inbound channel writes for this conversation's explicit scope."""
    if not chat_id:
        return
    with process_state().LOCK:
        st = _get_chat(chat_id)
        st.session_allows.add((KIND_MYSPACE, "", ""))
        st.last_ts = time.monotonic()


def get_ui_queue(chat_id: Optional[str]) -> Optional[asyncio.Queue]:
    """Get the chat's "show confirmation bar" signal queue; **does not create**
    it (the queue is created on demand only when gate actually needs to
    suspend). The stream consumer re-fetches it on every drain round — so even
    if _gc collected an old _ChatConfirm and gate rebuilt a new queue, the
    consumer never gets stuck on a discarded stale object.
    Returns None when there is currently no signal source to drain.
    """
    if not chat_id:
        return None
    with process_state().LOCK:
        st = process_state().CHATS.get(chat_id)
        return st.ui_signals if st is not None else None


def _pending_to_info(cid: str, p: _Pending, *, include_policy: bool = False) -> Dict[str, Any]:
    """Normalize into the frontend FileConfirmInfo / DesignPickInfo shape.

    design_pick additionally carries question/options — SSE push and GET
    pending recovery go through this same path, so the frontend can fully
    rebuild the picker after a refresh.
    """
    info: Dict[str, Any] = {
        "confirm_id": cid,
        "op": p.op,
        "logical_path": p.logical_path,
        "message": p.summary or p.logical_path,
        "kind": p.kind,
    }
    if include_policy and p.approval_user_id:
        info["_approval_user_id"] = p.approval_user_id
    if p.kind == KIND_DESIGN_PICK and p.payload:
        info["question"] = p.payload.get("question", "")
        info["options"] = p.payload.get("options", [])
    return info


def get_pending(chat_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The chat's **most recent** still-undecided (no decision) pending confirmation, else None.

    After a refresh / chat switch the frontend uses this (out-of-band endpoint)
    to restore the confirmation bar from the backend.
    """
    if not chat_id:
        return None
    with process_state().LOCK:
        st = process_state().CHATS.get(chat_id)
        if st is None or not st.pending:
            return None
        live = [(cid, p) for cid, p in st.pending.items() if p.decision is None]
        if not live:
            return None
        st.last_ts = time.monotonic()
        cid, p = max(live, key=lambda kv: kv[1].ts)
        return _pending_to_info(cid, p)


def get_all_pending(chat_id: Optional[str]) -> list[Dict[str, Any]]:
    """**All** still-undecided pending confirmations for the chat, ascending by registration time (FIFO display order).

    With parallel tool calls, one round may concurrently register N distinct
    pending confirmations; the frontend confirms them one by one as a queue,
    and after a refresh / chat switch restores the entire queue authoritatively
    from the backend without losing any item.
    """
    if not chat_id:
        return []
    with process_state().LOCK:
        st = process_state().CHATS.get(chat_id)
        if st is None or not st.pending:
            return []
        live = [(cid, p) for cid, p in st.pending.items() if p.decision is None]
        if not live:
            return []
        st.last_ts = time.monotonic()
        live.sort(key=lambda kv: kv[1].ts)
        return [_pending_to_info(cid, p) for cid, p in live]


def list_pending_chat_ids() -> list[str]:
    """All chat_ids that have undecided pending confirmations (the out-of-band batch endpoint uses this for ownership filtering)."""
    with process_state().LOCK:
        return [
            cid
            for cid, st in process_state().CHATS.items()
            if any((p.decision is None for p in st.pending.values()))
        ]


def has_pending(chat_id: Optional[str]) -> bool:
    """Whether this chat has a live suspended confirmation/design wait."""
    if not chat_id:
        return False
    with process_state().LOCK:
        st = process_state().CHATS.get(chat_id)
        return bool(st and any((p.decision is None for p in st.pending.values())))


def set_decision(
    chat_id: str,
    confirm_id: str,
    decision: str,
    option_id: Optional[str] = None,
    *,
    cascade: bool = True,
) -> Dict[str, Any]:
    """Called by the out-of-band endpoint: record the user's decision and **wake** the suspended tool coroutine.

    decision values branch on the pending item's kind:
    - myspace / automation: {allow, allow_session, deny}
    - design_pick: {choice, skip}; for choice, option_id is required and must be a valid option
    Returns {ok, decision, op, logical_path} or {ok: False, error}.
    """
    with process_state().LOCK:
        st = process_state().CHATS.get(chat_id)
        if st is None:
            return {"ok": False, "reason": "stale", "error": "该确认已失效（可能已超时或服务重启）"}
        p = st.pending.get(confirm_id)
        if p is None or p.decision is not None:
            return {
                "ok": False,
                "reason": "stale",
                "error": f"confirm_id 不存在或已过期: {confirm_id}",
            }
        if p.kind == KIND_DESIGN_PICK:
            if decision not in _DESIGN_DECISIONS:
                return {
                    "ok": False,
                    "reason": "bad_decision",
                    "error": f"非法 decision（design_pick）: {decision}",
                }
            if decision == DECISION_CHOICE:
                valid_ids = {str(o.get("id")) for o in (p.payload or {}).get("options", [])}
                if not option_id or str(option_id) not in valid_ids:
                    return {
                        "ok": False,
                        "reason": "bad_option",
                        "error": f"非法 option_id: {option_id}",
                    }
                p.choice = str(option_id)
        elif decision not in _DECISIONS:
            return {"ok": False, "reason": "bad_decision", "error": f"非法 decision: {decision}"}
        st.last_ts = time.monotonic()
        p.decision = decision
        ev = p
        op, lp = (p.op, p.logical_path)
        cascaded: list[str] = []
        cascade_evs: list[_Pending] = []
        if decision == DECISION_ALLOW_SESSION and cascade:
            granted = _session_scope(p.kind, p.logical_path)
            st.session_allows.add((p.kind, granted, p.approval_user_id or ""))
            for other_cid, op_p in st.pending.items():
                if other_cid == confirm_id or op_p.decision is not None:
                    continue
                if (
                    op_p.kind != p.kind
                    or op_p.kind == KIND_DESIGN_PICK
                    or op_p.approval_user_id != p.approval_user_id
                ):
                    continue
                if not _scope_covers(p.kind, granted, op_p.logical_path):
                    continue
                op_p.decision = DECISION_ALLOW_SESSION
                cascade_evs.append(op_p)
                cascaded.append(other_cid)
    for pending in [ev, *cascade_evs]:
        try:
            same_loop = asyncio.get_running_loop() is pending.loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            pending.event.set()
        else:
            pending.loop.call_soon_threadsafe(pending.event.set)
    return {"ok": True, "decision": decision, "op": op, "logical_path": lp, "cascaded": cascaded}


def _confirm_enabled(kind: str = KIND_MYSPACE) -> bool:
    try:
        from core.config.settings import settings as _s

        if kind == KIND_AUTOMATION:
            return bool(getattr(_s.sandbox, "automation_write_confirm", True))
        if (
            kind == KIND_LOCAL_CMD
            or kind.startswith(KIND_LOCAL_PATH_PREFIX)
            or kind == KIND_TOOL_PERMISSION
            or kind.startswith(KIND_TOOL_PREFIX)
        ):
            return True
        return bool(_s.sandbox.myspace_write_confirm)
    except Exception:
        return True


def _intercept_message(kind: str, phase: str, op: str, logical_path: str, summary: str) -> str:
    """Generate the model-facing intercept explanation by kind + phase (dedup/deny/timeout).

    Centralizes all kind-aware wording so gate() doesn't repeat ``if kind == ...``
    in three places. myspace identifies the target by logical path; automation
    by summary (e.g. "delete cron task 'daily report'").
    """
    if kind == KIND_AUTOMATION:
        tgt = summary or logical_path
        return {
            "dedup": f"同一定时任务操作（{tgt}）的并发重复调用已由首个调用完成，本次自动跳过——这是正常去重，不是错误，无需重试。",
            "deny": f"用户拒绝了该定时任务操作（{tgt}）。不要重试该操作，请向用户说明已取消，或澄清其真实意图后再操作。",
            "timeout": f"等待用户确认该定时任务操作超时（{tgt}），已放弃未执行。请简短告知用户超时，让其重新发起。",
        }[phase]
    if kind == KIND_LOCAL_CMD or kind.startswith(KIND_LOCAL_PATH_PREFIX):
        tgt = summary or logical_path
        return {
            "dedup": f"同一本机操作（{tgt}）的并发重复调用已由首个执行，本次自动跳过，无需重试。",
            "deny": f"用户拒绝了该本机操作（{tgt}）。不要重试，请向用户说明已取消，或澄清其真实意图后再操作。",
            "timeout": f"等待用户确认该本机操作超时（{tgt}），已放弃未执行。请简短告知用户超时。",
        }[phase]
    if kind == KIND_TOOL_PERMISSION or kind.startswith(KIND_TOOL_PREFIX):
        tgt = summary or logical_path
        return {
            "dedup": f"同一工具操作（{tgt}）的并发重复调用已由首个执行，本次自动跳过，无需重试。",
            "deny": f"用户拒绝了该工具操作（{tgt}）。不要重试，请向用户说明已取消。",
            "timeout": f"等待用户确认该工具操作超时（{tgt}），已放弃未执行。请简短告知用户超时。",
        }[phase]
    return {
        "dedup": f"同一文件（{logical_path}）的并发重复{op}已由首个调用完成，本次自动跳过以避免重复产物——这是正常去重，不是错误，无需重试。",
        "deny": f"用户拒绝了对「我的空间」的{op}操作（{logical_path}）。不要重试该写入。如确需产物，改写到本次会话的工作目录（相对路径），或向用户澄清意图。",
        "timeout": f"等待用户确认对「我的空间」的{op}操作超时（{logical_path}），已放弃未写入。请简短告知用户超时，让其重新发起或改写到会话工作目录。",
    }[phase]


@dataclass
class State:
    LOCK: object = field(default_factory=lambda: threading.RLock())
    CHATS: Dict[str, _ChatConfirm] = field(default_factory=lambda: {})


_STATE = State()


def process_state() -> State:
    return _STATE
