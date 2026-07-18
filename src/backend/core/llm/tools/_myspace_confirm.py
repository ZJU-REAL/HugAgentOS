"""User confirmation for MySpace write operations — Claude Code-shaped "true suspend" gate (docs §13).

Old implementation: an unapproved /myspace write returned an
``awaiting_user_confirmation`` sentinel ToolResponse, expecting the model to
relay "confirmation needed" to the user, and the same write was only persisted
by a **retry** after approval. Consequences (reproduced in trace fbd476f8): the
model treated the sentinel as an ordinary failure and retried repeatedly within
the same run's ReAct loop (4 Write calls in one run); after approval a separate
resume-after-confirm continuation run was still required; and a single-use
authorization consumed by a concurrent retry popped the confirmation again.

New implementation mirrors Claude Code
(``src/services/tools/toolExecution.ts::runToolUse``
+ ``hooks/toolPermission/handlers/interactiveHandler.ts``): an unapproved
/myspace write **suspends the current tool coroutine** — ``await`` on a
per-confirm ``asyncio.Event`` — while the frontend is told via SSE to show a
confirmation bar. The user's out-of-band
``POST /v1/chats/{id}/file-confirm`` triggers that Event → the **same tool
coroutine** wakes up and performs the write exactly once in place. The model
stays blocked inside the tool call the whole time (no tool_result yet), so it
physically cannot retry and no continuation run is needed.

Feasibility key (verified against routing/streaming.py): ``agent.reply()`` runs
in its own ``asyncio.create_task``; the consumer loop keeps SSE alive with an
independent heartbeat via ``asyncio.wait_for(...timeout)``. An ``await`` inside
a tool only suspends the agent task — it does not block the event loop or stall
SSE. Both conclusions in the old docstring ("AgentScope has no pre-tool hook /
blocking would deadlock the stream") no longer hold for the current architecture.

Concurrent dedup: with ``parallel_tool_calls=True`` the model may emit multiple
Write blocks concurrently in one reasoning step. Dedup by
``(op, logical_path)`` — concurrent/duplicate writes to the same file share
**one** pending + one Event + only **one** confirmation bar.

Storage: in-process per-chat (fine for single-worker dev; multi-worker needs
Redis + pub/sub, since ``asyncio.Event`` does not cross processes). Event/Queue
are created lazily and bound to the uvicorn event loop; gate (agent task) /
stream consumer / the /file-confirm endpoint share the same loop, so
cross-coroutine use is safe.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# "Kind" of confirmation gate — determines short-circuit conditions, toggle
# source, and intercept wording. MySpace writes and automation (cron) changes
# share the same suspend state machine (pending/event/ui_signals/dedup/decision),
# differing only in wording and frontend rendering. design_pick (site-building
# design pick-one-of-three) reuses the same state machine, but its semantics are
# "pick one of many" rather than "approve/deny": decision values, frontend
# component, and timeout behavior are all independent (see pick()); the
# multi-worker limitation is the same as other kinds (in-process Event does not
# cross processes).
KIND_MYSPACE = "myspace"
KIND_AUTOMATION = "automation"
KIND_DESIGN_PICK = "design_pick"

# op values (myspace writes)
OP_WRITE = "write"
OP_EDIT = "edit"
OP_DELETE = "delete"
OP_MOVE = "move"
OP_MKDIR = "mkdir"

# op values (automation/cron task changes, kind=automation)
OP_CRON_CREATE = "cron_create"
OP_CRON_UPDATE = "cron_update"
OP_CRON_DELETE = "cron_delete"

# Decision values (frontend useStreaming.ts and FileConfirmBar.tsx have matching literals; changing these requires syncing them)
DECISION_ALLOW = "allow"
DECISION_ALLOW_SESSION = "allow_session"
DECISION_DENY = "deny"
_DECISIONS = (DECISION_ALLOW, DECISION_ALLOW_SESSION, DECISION_DENY)
_DECISION_TIMEOUT = "_timeout"  # internal: wait timed out

# design_pick-specific decision values (frontend DesignPickerCard.tsx has matching literals)
DECISION_CHOICE = "choice"   # an option was selected (must carry option_id)
DECISION_SKIP = "skip"       # user clicked "let the assistant decide"
_DESIGN_DECISIONS = (DECISION_CHOICE, DECISION_SKIP)

# Non-interactive (batch/sub-agent) mode: reject the write outright; the model uses this to switch to /workspace
STATUS_BLOCKED = "blocked_non_interactive"

_TTL_S = 1800              # per-chat idle expiry for an **empty registry** (not collected while pending exists)
# Maximum tool suspend duration. While suspended the chat's pending set is
# non-empty so _gc will not collect it, hence this value may be far larger than
# _TTL_S. 2h covers the common "user steps away and comes back to confirm"
# case; still bounded — avoids an unbounded suspend pinning the agent task +
# SSE long connection forever.
_DEFAULT_WAIT_S = 7200     # 2 hours


@dataclass
class _Pending:
    op: str
    logical_path: str
    summary: str
    ts: float
    event: asyncio.Event
    decision: Optional[str] = None  # written by set_decision
    waiters: int = 0                # number of tool coroutines concurrently suspended after same-key dedup
    performed: bool = False         # whether "actually perform the write" has been claimed by some coroutine in this dedup group
    kind: str = KIND_MYSPACE        # myspace write / automation cron-task change / design_pick
    # design_pick-specific: payload={"question", "options"}, choice=selected option_id
    payload: Optional[Dict[str, Any]] = None
    choice: Optional[str] = None


@dataclass
class _ChatConfirm:
    # confirm_id → _Pending
    pending: Dict[str, _Pending] = field(default_factory=dict)
    # (op, logical_path) → confirm_id; concurrent/duplicate writes dedup to the same pending
    key_index: Dict[Tuple[str, str], str] = field(default_factory=dict)
    # "allow for this whole session": all subsequent /myspace writes in this chat pass directly
    session_allow: bool = False
    # the stream consumer uses this to push "show confirmation bar" events to the frontend (lazily created on the event loop)
    ui_signals: Optional[asyncio.Queue] = None
    last_ts: float = field(default_factory=time.monotonic)


_LOCK = threading.RLock()
_CHATS: Dict[str, _ChatConfirm] = {}


def _gc(now: float) -> None:
    """Lazily clean up expired chat state (call while holding the lock). Chats with pending items / suspended waiters are not cleaned."""
    dead = [
        c for c, st in _CHATS.items()
        if now - st.last_ts > _TTL_S and not st.pending
    ]
    for c in dead:
        _CHATS.pop(c, None)


def _get_chat(chat_id: str) -> _ChatConfirm:
    """Get or create per-chat state (call while holding the lock)."""
    st = _CHATS.get(chat_id)
    if st is None:
        st = _ChatConfirm()
        _CHATS[chat_id] = st
    return st


def allow_session(chat_id: Optional[str]) -> None:
    """Pre-mark the whole session as "allow for this session" — all subsequent
    /myspace writes pass directly through gate's ``if st.session_allow``, with
    no confirmation prompt and no suspend.

    Used for **inbound channel bots**: the bot runs as the owner, the person
    sending messages in a DM is the owner themself, and IM clients like Feishu
    have no approval UI (a gate suspend would hang until the 2h timeout and the
    placeholder message would never update). Equivalent to "the owner approving
    their own writes"; call once before the run starts.

    Note: the ``if not interactive`` check in gate comes before
    ``session_allow`` — so if a channel run spawns a sub-agent (isolated →
    non-interactive), its /myspace writes are still rejected as usual; only the
    channel's main agent is affected by this pre-authorization.
    """
    if not chat_id:
        return
    with _LOCK:
        st = _get_chat(chat_id)
        st.session_allow = True
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
    with _LOCK:
        st = _CHATS.get(chat_id)
        return st.ui_signals if st is not None else None


def _pending_to_info(cid: str, p: _Pending) -> Dict[str, Any]:
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
    with _LOCK:
        st = _CHATS.get(chat_id)
        if st is None or not st.pending:
            return None
        live = [
            (cid, p) for cid, p in st.pending.items() if p.decision is None
        ]
        if not live:
            return None
        # Refresh last_ts only when undecided items actually exist. Otherwise
        # the frontend polling this endpoint on every chat switch would keep an
        # empty-registry _ChatConfirm alive forever and _gc would never collect
        # it (memory leak).
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
    with _LOCK:
        st = _CHATS.get(chat_id)
        if st is None or not st.pending:
            return []
        live = [
            (cid, p) for cid, p in st.pending.items() if p.decision is None
        ]
        if not live:
            return []
        st.last_ts = time.monotonic()
        live.sort(key=lambda kv: kv[1].ts)
        return [_pending_to_info(cid, p) for cid, p in live]


def list_pending_chat_ids() -> list[str]:
    """All chat_ids that have undecided pending confirmations (the out-of-band batch endpoint uses this for ownership filtering)."""
    with _LOCK:
        return [
            cid for cid, st in _CHATS.items()
            if any(p.decision is None for p in st.pending.values())
        ]


def set_decision(
    chat_id: str,
    confirm_id: str,
    decision: str,
    option_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Called by the out-of-band endpoint: record the user's decision and **wake** the suspended tool coroutine.

    decision values branch on the pending item's kind:
    - myspace / automation: {allow, allow_session, deny}
    - design_pick: {choice, skip}; for choice, option_id is required and must be a valid option
    Returns {ok, decision, op, logical_path} or {ok: False, error}.
    """
    with _LOCK:
        st = _CHATS.get(chat_id)
        if st is None:
            # Chat not in the registry: almost always the confirmation timed
            # out and was collected, or a backend restart wiped in-process
            # state. Mark stale so the endpoint degrades gracefully instead of
            # hard-failing.
            return {"ok": False, "reason": "stale",
                    "error": "该确认已失效（可能已超时或服务重启）"}
        p = st.pending.get(confirm_id)
        if p is None:
            return {"ok": False, "reason": "stale",
                    "error": f"confirm_id 不存在或已过期: {confirm_id}"}
        # Locate the pending item first, then validate decision by kind — the
        # design_pick and approve/deny decision-value sets are mutually
        # exclusive; any cross-over is rejected, preventing a cascade or
        # misclick from waking the picker into an invalid state.
        if p.kind == KIND_DESIGN_PICK:
            if decision not in _DESIGN_DECISIONS:
                return {"ok": False, "reason": "bad_decision",
                        "error": f"非法 decision（design_pick）: {decision}"}
            if decision == DECISION_CHOICE:
                valid_ids = {
                    str(o.get("id"))
                    for o in (p.payload or {}).get("options", [])
                }
                if not option_id or str(option_id) not in valid_ids:
                    return {"ok": False, "reason": "bad_option",
                            "error": f"非法 option_id: {option_id}"}
                p.choice = str(option_id)
        else:
            if decision not in _DECISIONS:
                return {"ok": False, "reason": "bad_decision",
                        "error": f"非法 decision: {decision}"}
        st.last_ts = time.monotonic()
        p.decision = decision
        ev = p.event
        op, lp = p.op, p.logical_path
        # "Allow for this session": besides letting subsequent writes pass via
        # session_allow, we must also **cascade-wake** the other confirmation
        # coroutines already suspended right now. One parallel-tool-call round
        # concurrently registers N writes to different files, each awaiting its
        # own Event; those coroutines passed gate's `if st.session_allow` check
        # before the flag flipped, so a single set_decision won't wake them.
        # Without the cascade they hang until timeout — manifesting as "clicked
        # allow-for-session but later items still prompt one by one" (this bug).
        cascaded: list[str] = []
        cascade_evs: list[asyncio.Event] = []
        if decision == DECISION_ALLOW_SESSION:
            st.session_allow = True
            for other_cid, op_p in st.pending.items():
                if other_cid == confirm_id or op_p.decision is not None:
                    continue
                # design_pick is a "pick one of many" question, not a write
                # authorization — cascade-waking it with "allow for this
                # session" would produce an invalid decision; must skip it and
                # let it keep waiting.
                if op_p.kind == KIND_DESIGN_PICK:
                    continue
                op_p.decision = DECISION_ALLOW_SESSION
                cascade_evs.append(op_p.event)
                cascaded.append(other_cid)
    # set() outside the lock: wakes all tool coroutines in gate() waiting on these Events.
    ev.set()
    for cev in cascade_evs:
        cev.set()
    return {
        "ok": True, "decision": decision, "op": op, "logical_path": lp,
        # Other confirm_ids released by the cascade: the frontend uses this to clear the whole queue at once instead of prompting one by one.
        "cascaded": cascaded,
    }


def _confirm_enabled(kind: str = KIND_MYSPACE) -> bool:
    try:
        from core.config.settings import settings as _s
        if kind == KIND_AUTOMATION:
            return bool(getattr(_s.sandbox, "automation_write_confirm", True))
        return bool(_s.sandbox.myspace_write_confirm)
    except Exception:  # noqa: BLE001 — on config errors, conservatively still require confirmation
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
            "dedup": (
                f"同一定时任务操作（{tgt}）的并发重复调用已由首个调用完成，"
                f"本次自动跳过——这是正常去重，不是错误，无需重试。"
            ),
            "deny": (
                f"用户拒绝了该定时任务操作（{tgt}）。不要重试该操作，"
                f"请向用户说明已取消，或澄清其真实意图后再操作。"
            ),
            "timeout": (
                f"等待用户确认该定时任务操作超时（{tgt}），已放弃未执行。"
                f"请简短告知用户超时，让其重新发起。"
            ),
        }[phase]
    return {
        "dedup": (
            f"同一文件（{logical_path}）的并发重复{op}已由首个调用完成，"
            f"本次自动跳过以避免重复产物——这是正常去重，不是错误，无需重试。"
        ),
        "deny": (
            f"用户拒绝了对「我的空间」的{op}操作（{logical_path}）。"
            f"不要重试该写入。如确需产物，改写到沙盒 /workspace/ 下，"
            f"或向用户澄清意图。"
        ),
        "timeout": (
            f"等待用户确认对「我的空间」的{op}操作超时（{logical_path}），"
            f"已放弃未写入。请简短告知用户超时，让其重新发起或改写 /workspace/。"
        ),
    }[phase]


def _register_pending(
    cid_key: str,
    key: Tuple[str, str],
    *,
    make_pending,
    precheck=None,
) -> Tuple[Optional[tuple], Optional[str], Optional[_Pending]]:
    """Register (or dedup-reuse by key) a pending item and push a "show card" signal to the frontend.

    Registration skeleton shared by gate()/pick(). ``precheck(st)`` runs first
    inside **the same critical section**; returning ``(value,)`` short-circuits
    (gate's session_allow pass-through, pick's concurrent-second-question
    rejection), returning None continues registration. Returns
    ``(short, cid, p)``: a non-None short is the short-circuit value; otherwise
    cid/p are ready and waiters has been incremented. Only the "first
    registrant" pushes the UI signal — concurrent/duplicate calls dedup to the
    same pending and never show the card twice.
    """
    now = time.monotonic()
    ui_q: Optional[asyncio.Queue] = None
    ui_payload: Optional[Dict[str, Any]] = None
    with _LOCK:
        _gc(now)
        st = _get_chat(cid_key)
        st.last_ts = now
        if precheck is not None:
            short = precheck(st)
            if short is not None:
                return short, None, None
        existing_cid = st.key_index.get(key)
        p: Optional[_Pending] = (
            st.pending.get(existing_cid) if existing_cid else None
        )
        is_new = p is None or p.decision is not None
        if is_new:
            cid = uuid.uuid4().hex[:12]
            p = make_pending()
            st.pending[cid] = p
            st.key_index[key] = cid
        else:
            cid = existing_cid  # type: ignore[assignment]
        p.waiters += 1
        if is_new:
            if st.ui_signals is None:
                st.ui_signals = asyncio.Queue()
            ui_q = st.ui_signals
            ui_payload = _pending_to_info(cid, p)

    if ui_q is not None and ui_payload is not None:
        try:
            ui_q.put_nowait(ui_payload)
        except Exception:  # noqa: BLE001 — a queue error must not take down the tool
            logger.warning("[confirm] ui_signals put failed", exc_info=True)
    return None, cid, p


async def _wait_pending(
    cid_key: str,
    key: Tuple[str, str],
    cid: str,
    p: _Pending,
    *,
    timeout: float,
    expire_message: str,
    cancel_message: str,
    in_lock_extract=None,
) -> Tuple[str, Any]:
    """Suspend awaiting the user's decision; returns ``(decision, extracted)``.

    Finishing skeleton shared by gate()/pick(). Concurrency-critical (learned
    the hard way, do not split): reading decision, the ``in_lock_extract`` hook
    (gate's performed execution claim / pick's choice read), decrementing
    waiters, and cleaning pending/key_index must complete atomically in **the
    same critical section** — otherwise, when a wait_for timeout coincides with
    set_decision, the tool falsely sees a timeout (set_decision already
    reported success to the user while the tool discards the result as timed
    out), and the concurrent group's claim gets misaligned with the lifecycle.

    On timeout or run cancellation (stop button / regenerate / server-side
    cancel), the last waiter pushes an expire signal to the frontend to dismiss
    the UI card (otherwise it becomes a zombie: the pending item lingers
    forever and the user's next click on the confirm_id goes nowhere); in the
    cancellation case, CancelledError is **re-raised** after cleanup.
    """
    cancelled = False
    try:
        await asyncio.wait_for(p.event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        cancelled = True

    expire_q: Optional[asyncio.Queue] = None
    expire_payload: Optional[Dict[str, Any]] = None
    with _LOCK:
        decision = p.decision or _DECISION_TIMEOUT
        extracted = in_lock_extract(p, decision) if in_lock_extract else None
        p.waiters -= 1
        if p.waiters <= 0:
            st2 = _CHATS.get(cid_key)
            if st2 is not None:
                st2.pending.pop(cid, None)
                if st2.key_index.get(key) == cid:
                    st2.key_index.pop(key, None)
                if (
                    (cancelled or decision == _DECISION_TIMEOUT)
                    and st2.ui_signals is not None
                ):
                    expire_q = st2.ui_signals
                    expire_payload = {
                        "confirm_id": cid,
                        "op": p.op,
                        "logical_path": p.logical_path,
                        "kind": p.kind,
                        "expired": True,
                        "message": cancel_message if cancelled else expire_message,
                    }

    if expire_q is not None and expire_payload is not None:
        try:
            expire_q.put_nowait(expire_payload)
        except Exception:  # noqa: BLE001 — a queue error must not take down the tool
            logger.warning("[confirm] expire signal put failed", exc_info=True)

    if cancelled:
        raise asyncio.CancelledError()
    return decision, extracted


async def gate(
    *,
    chat_id: Optional[str],
    op: str,
    logical_path: str,
    interactive: bool,
    summary: str = "",
    timeout: float = _DEFAULT_WAIT_S,
    kind: str = KIND_MYSPACE,
) -> Optional[Dict[str, Any]]:
    """Pre-write gate (Claude Code-shaped) — shared by myspace writes and automation cron-task changes.

    Returns ``None`` = pass (caller proceeds to actually perform the
    operation); returns ``dict`` = intercepted (caller returns it to the model
    as-is and must **not** perform the operation).

    The caller is responsible for deciding "whether to gate at all" (myspace's
    /myspace ownership check and automation's channel/non-interactive skips
    happen in their respective wrapper layers); once here, confirmation is
    always applied as needed. Decision flow: toggle off → pass;
    non-interactive mode → deny; session already fully allowed → pass;
    otherwise register a pending confirmation + have the frontend show the
    confirmation bar via ui_signals, and **suspend the current tool coroutine**
    until the user decides out-of-band (or timeout).
    allow/allow_session → pass; deny/timeout → intercept.
    """
    if not _confirm_enabled(kind):
        return None
    if not interactive:
        return {
            "status": STATUS_BLOCKED,
            "op": op,
            "logical_path": logical_path,
            "error": (
                "非交互模式（批量/子智能体）禁止写用户「我的空间」。"
                "请改写到沙盒 /workspace/，或由用户在主对话中亲自操作。"
            ),
        }

    cid_key = chat_id or "_nochat_"

    def _session_allowed(st: _ChatConfirm):
        return (None,) if st.session_allow else None

    short, cid, p = _register_pending(
        cid_key,
        (op, logical_path),
        make_pending=lambda: _Pending(
            op=op,
            logical_path=logical_path,
            summary=summary or logical_path,
            ts=time.monotonic(),
            event=asyncio.Event(),
            kind=kind,
        ),
        precheck=_session_allowed,
    )
    if short is not None:
        return short[0]
    assert cid is not None and p is not None

    logger.info(
        "[myspace-confirm] 挂起等待用户确认 chat=%s op=%s path=%s confirm_id=%s",
        cid_key, op, logical_path, cid,
    )

    # Execution-dedup claim (in-lock hook): under parallel_tool_calls the model
    # often emits N identical Writes concurrently in one reasoning step
    # (MiniMax especially); dedup shares one confirm but there are still N
    # coroutines. Let the **first** coroutine actually perform the write
    # (return None) while the rest return a "deduplicated" success — avoiding N
    # duplicate artifacts. A genuinely different subsequent write is a separate
    # pending.
    def _claim_first(pd: _Pending, decision: str) -> bool:
        first = (
            decision in (DECISION_ALLOW, DECISION_ALLOW_SESSION)
            and not pd.performed
        )
        if first:
            pd.performed = True
        return first

    decision, first = await _wait_pending(
        cid_key, (op, logical_path), cid, p,
        timeout=timeout,
        expire_message="等待确认超时，已自动取消。如仍需要请重新发起。",
        cancel_message="本次运行已取消，该确认已关闭。",
        in_lock_extract=_claim_first,
    )

    if decision in (DECISION_ALLOW, DECISION_ALLOW_SESSION):
        if first:
            logger.info(
                "[myspace-confirm] 用户已批准(%s) chat=%s path=%s → 放行写入",
                decision, cid_key, logical_path,
            )
            return None
        logger.info(
            "[myspace-confirm] 并发重复去重跳过 chat=%s path=%s（首个协程已执行）",
            cid_key, logical_path,
        )
        return {
            "status": "deduplicated",
            "ok": True,
            "op": op,
            "logical_path": logical_path,
            "message": _intercept_message(kind, "dedup", op, logical_path, p.summary),
        }
    if decision == DECISION_DENY:
        return {
            "status": "denied_by_user",
            "op": op,
            "logical_path": logical_path,
            "error": _intercept_message(kind, "deny", op, logical_path, p.summary),
        }
    # Timeout
    return {
        "status": "confirm_timeout",
        "op": op,
        "logical_path": logical_path,
        "error": _intercept_message(kind, "timeout", op, logical_path, p.summary),
    }


async def pick(
    *,
    chat_id: Optional[str],
    question: str,
    options: list,
    interactive: bool,
    timeout: float = _DEFAULT_WAIT_S,
) -> Dict[str, Any]:
    """Site-building design pick-one-of-three: suspend the current tool coroutine until the user clicks a choice in the UI.

    Shares the pending/event/ui_signals state machine and the timeout expire
    path with gate(), but the semantics are "pick one of many": it does not
    check session_allow (that is a write-authorization short-circuit,
    meaningless for a question) and is not affected by the
    myspace_write_confirm toggle (the picker is not a safety confirmation but a
    required interaction).

    options: [{"id", "title", "brief"?, "image_file_id"}, ...] (already validated by the caller).
    Returns:
      {"status": "chosen", "option_id": "..."}   user selected an option
      {"status": "skipped"}                      user clicked "let the assistant decide"
      {"status": "timeout"}                      wait timed out (expire signal already pushed to dismiss the UI)
      {"status": STATUS_BLOCKED, "error": ...}   non-interactive mode (no human in the loop)
    """
    if not interactive:
        return {
            "status": STATUS_BLOCKED,
            "error": (
                "当前为非交互模式（批量/子智能体/渠道机器人），无法弹出"
                "设计选择器。请直接选择你认为最合适的方案继续。"
            ),
        }

    cid_key = chat_id or "_nochat_"
    key = ("design_pick", (question or "")[:120])

    def _reject_concurrent(st: _ChatConfirm):
        # The same chat already has a picker for **a different question**
        # suspended → reject the concurrent second question. The frontend
        # picker is single-slot rendered; a second one would overwrite the
        # first, whose coroutine would then hang until timeout; the SKILL also
        # states one site-building session asks at most one round. (Concurrent
        # calls with the same question never reach here — key_index dedup hits
        # and reuses after precheck.)
        for ocid, other in st.pending.items():
            if (
                other.kind == KIND_DESIGN_PICK
                and other.decision is None
                and st.key_index.get(key) != ocid
            ):
                return ({
                    "status": "already_pending",
                    "error": (
                        "已有一个设计方案选择正在等用户操作，禁止并发发起"
                        "第二个。请等待当前选择结果，按其返回继续。"
                    ),
                },)
        return None

    short, cid, p = _register_pending(
        cid_key,
        key,
        make_pending=lambda: _Pending(
            op="design_pick",
            logical_path=key[1],
            summary=question or "请选择一个设计方案",
            ts=time.monotonic(),
            event=asyncio.Event(),
            kind=KIND_DESIGN_PICK,
            payload={"question": question, "options": options},
        ),
        precheck=_reject_concurrent,
    )
    if short is not None:
        return short[0]
    assert cid is not None and p is not None

    logger.info(
        "[design-pick] 挂起等待用户选择 chat=%s confirm_id=%s options=%d",
        cid_key, cid, len(options),
    )

    # The dedup group shares the same choice; reading it in-lock suffices, no performed claim needed.
    decision, choice = await _wait_pending(
        cid_key, key, cid, p,
        timeout=timeout,
        expire_message="设计方案选择已超时，助手将自行选择方案继续。",
        cancel_message="本次运行已取消，设计方案选择已关闭。",
        in_lock_extract=lambda pd, _d: pd.choice,
    )

    if decision == DECISION_CHOICE and choice:
        logger.info("[design-pick] 用户选中 chat=%s option=%s", cid_key, choice)
        return {"status": "chosen", "option_id": choice}
    if decision == DECISION_SKIP:
        return {"status": "skipped"}
    return {"status": "timeout"}
