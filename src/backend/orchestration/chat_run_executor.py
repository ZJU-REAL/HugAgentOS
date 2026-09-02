"""Chat Run Executor — decouples the AI workflow from the HTTP connection lifecycle.

Each sent message creates a ChatRun row + starts a background asyncio.Task. The
task consumes ``astream_chat_workflow``, converts every chunk into an SSE event
and XADDs it to the Redis Stream ``jx:chat:run:{run_id}:events``. SSE followers
read the stream via XRANGE replay + XREAD tailing, which enables "resume the
live stream after a page refresh".

Public interface:
- ``start_run(...)``          launch a run already accepted by ChatSequencer
- ``wait_run(run_id)``        wait for a locally launched run without detaching it
- ``follow_run(run_id, ...)`` SSE follower: read events from the Redis Stream
- ``cancel_run(run_id, ...)`` cancel the background task + mark status=cancelled
- ``recover_orphan_runs()``   startup hook: resume from the last durable safe point
- ``get_run(run_id)``         read the ChatRun row (used by the route layer for authz)
- ``get_active_run_for_chat(chat_id, user_id)``  probe for an in-progress run
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Literal, Optional

from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.db.engine import SessionLocal
from core.db.models import ChatMessage, ChatRun
from core.harness.hooks import HookPaused, find_hook_paused
from core.infra.logging import get_logger
from core.infra.redis import get_redis
from core.llm import human_interaction
from core.services import ChatService
from core.services.run_journal import RecoveryDecision, RunJournal, RunLeaseLost
from core.services.tool_effect_ledger import ToolOutcomeUnknown, find_tool_outcome_unknown
from orchestration.workflow import astream_chat_workflow
from redis.exceptions import TimeoutError as RedisTimeoutError

logger = get_logger(__name__)


# ─── Types / constants ─────────────────────────────────────────────────

RunKind = Literal["chat", "plan_execute", "plan_generate", "autonomous_loop"]

# Kinds that use cooperative cancellation (no cross-task cancel; they poll is_run_cancelled and stop themselves, avoiding the anyio cancel-scope deadlock)
_COOPERATIVE_KINDS: tuple[str, ...] = ("plan_execute", "autonomous_loop")
RunStatus = Literal["pending", "running", "needs_attention", "completed", "failed", "cancelled"]

_TERMINAL_STATUSES: tuple[RunStatus, ...] = (
    "needs_attention",
    "completed",
    "failed",
    "cancelled",
)
_LIVE_STATUSES: tuple[RunStatus, ...] = ("pending", "running")

_STREAM_KEY = "jx:chat:run:{run_id}:events"
_STREAM_MAXLEN = 5000
_STREAM_TTL_SECONDS = human_interaction.STREAM_TTL_SECONDS
_TERMINAL_TYPE = "__terminal__"
_XREAD_BLOCK_MS = 5000
# When the SSE stream is silent longer than this, write a `: heartbeat\n\n`
# comment line on the wire so that nginx `proxy_read_timeout` (default 60s,
# 300s in this project) / intermediate reverse proxies / client-side proxies
# don't treat the idle stream during a long LLM call as a dead connection and
# kill it. The EventSource standard discards SSE comment lines, so the
# frontend needs no changes at all.
_HEARTBEAT_INTERVAL_SEC = 15.0

# Per-run "no activity" watchdog: if astream_chat_workflow produces no chunk
# within this many seconds (no yield, no raise, not cancelled) it is judged
# hung; a TimeoutError is raised and handled by the existing except path that
# writes the failed terminal state, so the run never stays in running forever.
_INACTIVITY_TIMEOUT_SEC = float(os.getenv("CHAT_RUN_INACTIVITY_TIMEOUT_SEC", "600"))
# Defense in depth: periodically check runs that are running and older than
# this age (backstop for the watchdog, also cleans up historical zombie runs).
# Exceeding the age alone no longer kills the run — the Redis Stream must also
# have been quiet for more than CHAT_RUN_STALE_QUIET_SEC (see reap_stale_runs);
# long tasks that are still producing output are not killed.
_STALE_RUN_MAX_AGE_SEC = float(os.getenv("CHAT_RUN_MAX_AGE_SEC", "1800"))
_STALE_REAPER_INTERVAL_SEC = float(os.getenv("CHAT_RUN_REAPER_INTERVAL_SEC", "300"))
# "Quiet" threshold for over-age runs: a run only counts as a zombie when the
# last event on its stream is older than this. Defaults to the same value as
# the per-run inactivity watchdog — runs hung inside this process get reaped
# by the watchdog first, so the only quiet runs reaching the reaper are those
# whose worker has vanished (leftovers from a process restart/crash).
_STALE_QUIET_SEC = float(os.getenv("CHAT_RUN_STALE_QUIET_SEC", str(_INACTIVITY_TIMEOUT_SEC)))
# Absolute lifetime cap: even a run that keeps actively producing output is
# force-reaped past this age, preventing a runaway agent loop from living
# forever by continuously emitting.
_HARD_MAX_AGE_SEC = float(os.getenv("CHAT_RUN_HARD_MAX_AGE_SEC", "21600"))
_RUN_LEASE_SECONDS = max(15, int(os.getenv("CHAT_RUN_LEASE_SECONDS", "90")))
_RUN_LEASE_HEARTBEAT_SEC = max(1.0, _RUN_LEASE_SECONDS / 3)
_RUN_RECOVERY_INTERVAL_SEC = max(
    1.0,
    float(os.getenv("CHAT_RUN_RECOVERY_INTERVAL_SEC", "15")),
)
_WORKER_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


async def _aiter_with_inactivity_timeout(
    aiter: AsyncIterator[Any],
    timeout: float,
    *,
    is_activity: Callable[[Any], bool] | None = None,
):
    """Yield from an async iterator and enforce a meaningful-activity deadline.

    Guards against a hung workflow (model loop / blocked MCP / asyncio
    deadlock) leaving the chat run stuck in 'running' forever — on timeout
    the underlying generator is closed and the error propagates into the
    existing ``except Exception`` path that writes the ``failed`` terminal.

    Transport keep-alives may still be yielded to the caller, but they do not
    reset the deadline when ``is_activity`` marks them as non-meaningful.
    """
    activity_check = is_activity or (lambda _: True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            with contextlib.suppress(Exception):
                await aiter.aclose()  # type: ignore[attr-defined]
            raise TimeoutError(f"工作流 {timeout:.0f}s 内无有效输出，已判定为卡死并中止")
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                await aiter.aclose()  # type: ignore[attr-defined]
            raise TimeoutError(f"工作流 {timeout:.0f}s 内无有效输出，已判定为卡死并中止") from exc
        if activity_check(item):
            deadline = loop.time() + timeout
        yield item


def _is_chat_run_activity(item: Any, chat_id: str) -> bool:
    """Classify workflow output for the inactivity watchdog.

    A transport heartbeat is normally not meaningful progress. It becomes
    progress only while a registered tool is legitimately suspended on a
    human answer; this keeps true HITL waits alive without weakening zombie
    detection for every other silent run.
    """

    return item.get("type") != "heartbeat" or human_interaction.has_pending(chat_id)


def _stream_key(run_id: str) -> str:
    return _STREAM_KEY.format(run_id=run_id)


# run_id → asyncio.Task — for cancel_run to kill the underlying coroutine
_active_runs: Dict[str, asyncio.Task] = {}


class ChatRunNotFound(Exception):
    pass


class ChatRunPermissionDenied(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _journal() -> RunJournal:
    # Resolve SessionLocal at call time so tests and alternate runtime profiles
    # can replace the factory without leaving the journal bound to another DB.
    return RunJournal(SessionLocal)


def _new_worker_owner(run_id: str) -> str:
    return f"{_WORKER_INSTANCE_ID}:{run_id}:{uuid.uuid4().hex[:12]}"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


async def _lease_heartbeat(
    run_id: str,
    owner: str,
    worker: asyncio.Task,
    lease_lost: asyncio.Event,
) -> None:
    """Renew ownership and fence the local coroutine immediately on lease loss."""
    interval = _RUN_LEASE_HEARTBEAT_SEC
    try:
        while True:
            await asyncio.sleep(interval)
            # ``renew`` is a synchronous UPDATE on chat_runs. Run it off the
            # event loop: whenever that row is locked by another transaction
            # this call waits inside the driver, and waiting *on the loop*
            # freezes every coroutine in the process — including the one
            # holding the lock, which then can never commit and release it.
            # A blocked heartbeat must only cost this run its lease, never the
            # whole backend.
            renewed = await asyncio.to_thread(
                _journal().renew,
                run_id,
                owner=owner,
                lease_seconds=_RUN_LEASE_SECONDS,
            )
            if not renewed:
                logger.warning("chat_run_lease_lost", run_id=run_id, owner=owner)
                lease_lost.set()
                worker.cancel()
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # DB/driver failures must fail closed
        logger.error(
            "chat_run_lease_renew_failed",
            run_id=run_id,
            owner=owner,
            error=str(exc),
            exc_info=True,
        )
        lease_lost.set()
        worker.cancel()


def _update_run_status(run_id: str, **fields: Any) -> None:
    """Single-shot UPDATE on chat_runs; isolated session so it doesn't pollute caller's txn."""
    if not fields:
        return
    if fields.get("status") in _TERMINAL_STATUSES:
        fields.setdefault("writer_slot", None)
    with SessionLocal() as db:
        affected = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == run_id)
            .update(fields, synchronize_session=False)
        )
        if not affected:
            logger.warning("chat_run_update_missing", run_id=run_id, fields=list(fields.keys()))
        db.commit()


def _claim_run_execution(run_id: str) -> bool:
    """Fence duplicate initial workers before acquiring the durable lease."""

    with SessionLocal() as db:
        affected = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == run_id, ChatRun.status == "pending")
            .update(
                {"status": "running", "started_at": _utcnow()},
                synchronize_session=False,
            )
        )
        db.commit()
    return bool(affected)


def _finalize_run(
    run_id: str,
    *,
    require_expired_lease: bool = False,
    **fields: Any,
) -> bool:
    """CAS variant of writing the terminal state: only updates while the run is still live.

    Prevents the race where "the reaper / cancel has already moved the run to a
    terminal state, and the worker's late completed/failed overwrites it back" —
    the side that loses the race abandons its write and returns False.
    """
    fields.setdefault("lease_owner", None)
    fields.setdefault("lease_expires_at", None)
    if fields.get("error_message") and not fields.get("failure_reason"):
        fields["failure_reason"] = fields["error_message"]
    if fields.get("status") in _TERMINAL_STATUSES:
        fields.setdefault("writer_slot", None)
    with SessionLocal() as db:
        query = db.query(ChatRun).filter(
            ChatRun.run_id == run_id,
            ChatRun.status.in_(_LIVE_STATUSES),
        )
        if require_expired_lease:
            from sqlalchemy import or_

            now = _utcnow()
            query = query.filter(
                or_(
                    ChatRun.lease_owner.is_(None),
                    ChatRun.lease_expires_at.is_(None),
                    ChatRun.lease_expires_at <= now,
                )
            )
        affected = query.update(fields, synchronize_session=False)
        db.commit()
    if not affected:
        logger.info("chat_run_finalize_skipped", run_id=run_id, fields=list(fields.keys()))
    return bool(affected)


def _request_run_stop(
    run_id: str,
    *,
    status: RunStatus,
    error_message: Optional[str] = None,
    require_expired_lease: bool = False,
) -> bool:
    """Write an external terminal verdict while retaining the writer fence."""

    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"not a terminal status: {status}")
    fields: Dict[str, Any] = {"status": status, "completed_at": _utcnow()}
    if error_message:
        fields["error_message"] = error_message[:1000]
        fields["failure_reason"] = error_message[:1000]
    with SessionLocal() as db:
        query = db.query(ChatRun).filter(
            ChatRun.run_id == run_id,
            ChatRun.status.in_(_LIVE_STATUSES),
        )
        if require_expired_lease:
            from sqlalchemy import or_

            now = _utcnow()
            query = query.filter(
                or_(
                    ChatRun.lease_owner.is_(None),
                    ChatRun.lease_expires_at.is_(None),
                    ChatRun.lease_expires_at <= now,
                )
            )
        affected = query.update(fields, synchronize_session=False)
        db.commit()
    return bool(affected)


def _request_run_cancel(run_id: str) -> bool:
    return _request_run_stop(run_id, status="cancelled")


def _acknowledge_terminal_writer(run_id: str) -> bool:
    """Release the writer fence only after the owning worker has stopped writing."""

    with SessionLocal() as db:
        affected = (
            db.query(ChatRun)
            .filter(
                ChatRun.run_id == run_id,
                ChatRun.status.in_(_TERMINAL_STATUSES),
                ChatRun.writer_slot.isnot(None),
            )
            .update({"writer_slot": None}, synchronize_session=False)
        )
        db.commit()
    return bool(affected)


def _acknowledge_never_started_terminal_writer(run_id: str) -> bool:
    """Release a terminal fence only when no worker ever claimed the run.

    A duplicate worker can lose the pending-to-running claim while the real
    worker is active, then observe a concurrent cancellation.  It must not
    release the real worker's fence in that case.  ``started_at IS NULL`` is
    the durable proof that there is no owning worker to wait for.
    """

    with SessionLocal() as db:
        affected = (
            db.query(ChatRun)
            .filter(
                ChatRun.run_id == run_id,
                ChatRun.status.in_(_TERMINAL_STATUSES),
                ChatRun.writer_slot.isnot(None),
                ChatRun.started_at.is_(None),
            )
            .update({"writer_slot": None}, synchronize_session=False)
        )
        db.commit()
    return bool(affected)


def _create_run_record(
    *,
    chat_id: str,
    user_id: str,
    request_payload: Dict[str, Any],
    recovery_snapshot: Optional[Dict[str, Any]] = None,
) -> ChatRun:
    """Durably accept a run before any worker task can be spawned."""
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    message_id = f"msg_{uuid.uuid4().hex[:16]}"
    return _journal().accept(
        run_id=run_id,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        request_payload=_json_safe(request_payload),
        recovery_snapshot=_json_safe(recovery_snapshot or {}),
    )


def _register_run_task(run_id: str, coro: Awaitable[None], *, name: str) -> None:
    """Spawn the worker coroutine, register it in _active_runs, auto-cleanup on done."""
    task = asyncio.create_task(coro, name=name)
    _active_runs[run_id] = task
    task.add_done_callback(lambda _t: _active_runs.pop(run_id, None))


async def _write_terminal_to_stream(
    run_id: str,
    *,
    chat_id: str,
    error_text: str,
    cancelled: bool = False,
) -> None:
    """Write an error event + terminal marker + EXPIRE.

    Used when the worker isn't around to emit them itself (cross-process cancel,
    orphan recovery). Failure is logged but not raised.
    """
    err_event: Dict[str, Any] = {
        "type": "error",
        "error": error_text,
        "delta": error_text,
        "chat_id": chat_id,
    }
    term_event: Dict[str, Any] = {"type": _TERMINAL_TYPE, "chat_id": chat_id}
    if cancelled:
        err_event["_cancelled"] = True
        term_event["_cancelled"] = True
    try:
        journal = _journal()
        error_offset = journal.allocate_event_offset(run_id, terminal=True)
        await _xadd_event(run_id, error_offset, err_event)
        terminal_offset = journal.allocate_event_offset(run_id, terminal=True)
        await _xadd_event(run_id, terminal_offset, term_event)
        await _expire_stream(run_id)
    except Exception as exc:
        logger.warning("chat_run_terminal_write_failed", run_id=run_id, error=str(exc))


# ─── Entry point: start_run (chat mode) ─────────────────────────────────


async def start_run(
    *,
    accepted_run: Optional[ChatRun] = None,
    chat_id: str,
    user_id: str,
    session_messages: List[Dict[str, Any]],
    effective_user_message: str,
    raw_user_message: str,
    context: Dict[str, Any],
    request_payload: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None,
) -> ChatRun:
    """Launch a ChatRun that ChatSequencer has already durably accepted.

    Acceptance (user message + pending run) must commit before this function is
    called.  Keeping launch separate makes a process crash between the two
    recoverable by the Run Journal in #64 instead of losing the request.
    """
    recovery_snapshot = _json_safe(
        {
            "kind": "chat",
            "worker_args": {
                "session_messages": session_messages,
                "effective_user_message": effective_user_message,
                "raw_user_message": raw_user_message,
                "context": context,
                "model_name": model_name,
            },
        }
    )
    if accepted_run is None:
        run = _create_run_record(
            chat_id=chat_id,
            user_id=user_id,
            request_payload=dict(request_payload or {}),
            recovery_snapshot=recovery_snapshot,
        )
    else:
        run = accepted_run
        if run.chat_id != chat_id or run.user_id != user_id or run.status != "pending":
            raise ValueError("accepted_run does not match the launch request")
        with SessionLocal() as db:
            durable_run = db.get(ChatRun, run.run_id)
            if durable_run is None or durable_run.status != "pending":
                raise ValueError("accepted_run is no longer pending")
            durable_run.recovery_snapshot = recovery_snapshot
            durable_run.snapshot_version = max(int(durable_run.snapshot_version or 0), 1)
            durable_run.run_phase = durable_run.run_phase or "accepted"
            durable_run.last_operation_safety = durable_run.last_operation_safety or "replayable"
            durable_run.updated_at = _utcnow()
            db.commit()
    journal_owner = _new_worker_owner(run.run_id)
    _register_run_task(
        run.run_id,
        _run_workflow(
            run_id=run.run_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=run.message_id,
            assistant_chat_seq=run.assistant_chat_seq,
            session_messages=session_messages,
            effective_user_message=effective_user_message,
            raw_user_message=raw_user_message,
            context=context,
            model_name=model_name,
            journal_owner=journal_owner,
        ),
        name=f"chat_run:{run.run_id}",
    )
    logger.info("chat_run_started", run_id=run.run_id, chat_id=chat_id, user_id=user_id)
    return run


def _commit_queued_handoff_in_session(
    db,
    *,
    source_run_id: str,
    chat_id: str,
    user_id: str,
    session_messages: List[Dict[str, Any]],
    assistant_content: str,
    context: Dict[str, Any],
    model_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Atomically consume one follow-up/next-run item and stage its ChatRun."""
    from core.db.models.chat import reserve_chat_sequences
    from core.services.steer_queue import SteerQueue

    source_run = db.get(ChatRun, source_run_id)
    if source_run is None:
        return None
    root_run_id = str(context.get("handoff_root_run_id") or source_run_id)
    handoff = SteerQueue.consume_next_handoff_in_session(
        db,
        source_run_id=source_run_id,
        root_run_id=root_run_id,
        chat_id=chat_id,
        applied_operation_seq=int(source_run.operation_seq or 0) + 1,
    )
    if handoff is None or not handoff.applied_run_id:
        return None

    first_seq = reserve_chat_sequences(
        db,
        chat_id,
        count=2,
        owner_user_id=user_id,
    )
    next_run_id = handoff.applied_run_id
    next_message_id = (
        f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{handoff.queue_id}:assistant').hex[:16]}"
    )
    queued_user_message_id = (
        f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{handoff.queue_id}:user').hex[:16]}"
    )
    next_context = _json_safe(dict(context or {}))
    next_context.pop("journal_owner", None)
    next_context.update(
        {
            "run_id": next_run_id,
            "message_id": next_message_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "handoff_queue_id": handoff.queue_id,
            "handoff_delivery_mode": handoff.delivery_mode,
            "handoff_root_run_id": root_run_id,
        }
    )
    next_session_messages = _json_safe(list(session_messages or []))
    if assistant_content:
        next_session_messages.append({"role": "assistant", "content": assistant_content})
    next_session_messages.append({"role": "user", "content": handoff.message})
    worker_args = {
        "session_messages": next_session_messages,
        "effective_user_message": handoff.message,
        "raw_user_message": handoff.message,
        "context": next_context,
        "model_name": model_name,
    }
    request_payload = dict(source_run.request_payload or {})
    request_payload.update(
        {
            "message": handoff.message,
            "chat_id": chat_id,
            "attachments": [],
            "handoff": {
                "queue_id": handoff.queue_id,
                "steer_id": handoff.steer_id,
                "delivery_mode": handoff.delivery_mode,
                "source_run_id": source_run_id,
            },
        }
    )
    RunJournal.accept_in_session(
        db,
        run_id=next_run_id,
        message_id=next_message_id,
        chat_id=chat_id,
        user_id=user_id,
        request_payload=_json_safe(request_payload),
        recovery_snapshot={"kind": "chat", "worker_args": _json_safe(worker_args)},
        user_message_id=queued_user_message_id,
        user_chat_seq=first_seq,
        assistant_chat_seq=first_seq + 1,
        writer_slot="main",
    )
    ChatService(db).upsert_message(
        chat_id=chat_id,
        role="user",
        content=handoff.message,
        model=model_name,
        message_id=queued_user_message_id,
        chat_seq=first_seq,
        extra_data={
            "queued_handoff": True,
            "steer_queue_id": handoff.queue_id,
            "steer_id": handoff.steer_id,
            "steer_seq": handoff.steer_seq,
            "delivery_mode": handoff.delivery_mode,
            "source_run_id": source_run_id,
            "run_id": next_run_id,
        },
        commit=False,
    )
    return {
        "run_id": next_run_id,
        "message_id": next_message_id,
        "user_message_id": queued_user_message_id,
        "assistant_chat_seq": first_seq + 1,
        "queue_id": handoff.queue_id,
        "steer_id": handoff.steer_id,
        "steer_seq": handoff.steer_seq,
        "delivery_mode": handoff.delivery_mode,
        **worker_args,
    }


def _schedule_queued_handoff(
    handoff: Dict[str, Any],
    *,
    chat_id: str,
    user_id: str,
    task_prefix: str = "chat_run_handoff",
) -> str:
    """Start a committed handoff; pending DB state remains recovery-safe on failure."""
    next_run_id = str(handoff["run_id"])
    next_owner = _new_worker_owner(next_run_id)
    _register_run_task(
        next_run_id,
        _run_workflow(
            run_id=next_run_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=str(handoff["message_id"]),
            assistant_chat_seq=(
                int(handoff["assistant_chat_seq"])
                if handoff.get("assistant_chat_seq") is not None
                else None
            ),
            session_messages=list(handoff.get("session_messages") or []),
            effective_user_message=str(handoff.get("effective_user_message") or ""),
            raw_user_message=str(handoff.get("raw_user_message") or ""),
            context=dict(handoff.get("context") or {}),
            model_name=(str(handoff["model_name"]) if handoff.get("model_name") else None),
            journal_owner=next_owner,
        ),
        name=f"{task_prefix}:{next_run_id}",
    )
    return next_run_id


def _queued_run_started_event(
    source_run_id: str,
    chat_id: str,
    handoff: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": "queued_run_started",
        "chat_id": chat_id,
        "source_run_id": source_run_id,
        "run_id": str(handoff["run_id"]),
        "message_id": handoff["message_id"],
        "user_message_id": handoff["user_message_id"],
        "message": handoff["raw_user_message"],
        "queue_id": handoff["queue_id"],
        "steer_id": handoff["steer_id"],
        "steer_seq": handoff["steer_seq"],
        "delivery_mode": handoff["delivery_mode"],
    }


async def _write_queued_run_started_projection(
    source_run_id: str,
    *,
    chat_id: str,
    handoff: Dict[str, Any],
) -> None:
    """Best-effort event projection; the queue API is the durable backfill seam."""
    try:
        offset = _journal().allocate_event_offset(source_run_id, terminal=True)
        await _xadd_event(
            source_run_id,
            offset,
            _queued_run_started_event(source_run_id, chat_id, handoff),
        )
    except Exception as exc:  # Redis projection must not undo the committed handoff
        logger.warning(
            "chat_run_handoff_projection_failed",
            run_id=source_run_id,
            child_run_id=handoff.get("run_id"),
            error=str(exc),
        )


async def wait_run(run_id: str) -> ChatRun:
    """Wait for a locally launched run and return its durable terminal row.

    Shielding prevents a disconnected non-stream HTTP client from cancelling
    the detached worker. Explicit ``cancel_run`` still targets the registered
    task and keeps the database writer fence until that task has stopped.
    """

    task = _active_runs.get(run_id)
    if task is not None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                # The waiter (for example a disconnected HTTP request) was
                # cancelled; shielding keeps the worker alive, and the caller
                # must still observe its own cancellation.
                raise
            # Explicit run cancellation stopped the worker; the durable row
            # below is the authoritative outcome.
    run = get_run(run_id)
    if run is None:
        raise ChatRunNotFound(f"chat run {run_id} not found")
    if run.status in _LIVE_STATUSES:
        raise RuntimeError(f"chat run {run_id} lost its local worker before completion")
    return run


# ─── Redis Stream writes ───────────────────────────────────────────────


async def _xadd_event(run_id: str, offset: int, event: Dict[str, Any]) -> None:
    """Serialize an SSE event and write it to the Redis Stream."""
    payload = {**event, "_offset": offset}
    redis = get_redis()
    try:
        await redis.xadd(
            _stream_key(run_id),
            {"data": json.dumps(payload, ensure_ascii=False)},
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:
        logger.warning("chat_run_xadd_failed", run_id=run_id, error=str(exc))


async def _expire_stream(run_id: str) -> None:
    redis = get_redis()
    try:
        await redis.expire(_stream_key(run_id), _STREAM_TTL_SECONDS)
    except Exception as exc:
        logger.warning("chat_run_expire_failed", run_id=run_id, error=str(exc))


async def _reset_stream_projection(run_id: str) -> None:
    """Drop a crashed worker's partial projection before replaying its run."""
    redis = get_redis()
    try:
        await redis.delete(_stream_key(run_id))
    except Exception as exc:
        logger.warning("chat_run_projection_reset_failed", run_id=run_id, error=str(exc))


# ─── Background worker ─────────────────────────────────────────────────


def _cancelled_by_user(run_id: str) -> bool:
    """租约被清掉有两种可能：别的 worker 接管了恢复，或者用户按了停止。

    cancel_run 会先把行写成终态 cancelled 再清租约，之后不会再有人接手这一轮 ——
    被围栏的 worker 若也闭嘴，用户已经看到的半截回答就没人落库了。
    """
    try:
        current = get_run(run_id)
    except Exception:  # noqa: BLE001 - 读不到就按"别人接管"处理，宁可不写
        return False
    return current is not None and current.status == "cancelled"


async def _run_workflow(
    *,
    run_id: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    session_messages: List[Dict[str, Any]],
    effective_user_message: str,
    raw_user_message: str,
    context: Dict[str, Any],
    model_name: Optional[str],
    assistant_chat_seq: Optional[int] = None,
    journal_owner: Optional[str] = None,
    recovering: bool = False,
) -> None:
    """Consume astream_chat_workflow, forward chunks to the Redis Stream, persist at the end.

    Every chunk is written to ``jx:chat:run:{run_id}:events`` via ``_emit``.
    The SSE event format stays fully identical to the existing protocol in
    chats.py:889-1087; the frontend needs no changes to its chunk parsing —
    only the new ``run_started`` event type is added.
    """
    from core.chat.context import now_iso, resolve_user_facing_error
    from core.chat.tool_log import (
        SegmentRecorder,
        attach_subagent_step,
        build_thinking_event,
        build_tool_call_delta_event,
        build_tool_call_event,
        build_tool_call_start_event,
        build_tool_result_event,
        build_user_question_event,
        build_user_question_resolved_event,
    )
    from core.chat.display_bounds import bound_result_for_display
    from core.services.artifact_service import persist_artifacts as _persist_artifacts

    initial_claimed = False
    if not recovering:
        initial_claimed = _claim_run_execution(run_id)
        if not initial_claimed:
            _acknowledge_never_started_terminal_writer(run_id)
            return

    owner = journal_owner or _new_worker_owner(run_id)
    journal = _journal()
    if not journal.claim(run_id, owner=owner, lease_seconds=_RUN_LEASE_SECONDS):
        logger.info("chat_run_claim_skipped", run_id=run_id, owner=owner)
        if initial_claimed:
            _acknowledge_terminal_writer(run_id)
        return
    try:
        journal.append_operation(
            run_id,
            owner=owner,
            operation_type="worker_started",
            phase="pre_model",
            safety="replayable",
        )
    except RunLeaseLost:
        logger.warning("chat_run_worker_fenced_before_start", run_id=run_id, owner=owner)
        return
    worker_task = asyncio.current_task()
    if worker_task is None:  # pragma: no cover - asyncio always owns this coroutine
        raise RuntimeError("chat run worker has no asyncio task")
    lease_lost = asyncio.Event()
    lease_task = asyncio.create_task(
        _lease_heartbeat(run_id, owner, worker_task, lease_lost),
        name=f"chat_run_lease:{run_id}",
    )
    # 本轮回答总耗时起点 —— 持久化进 extra_data.duration_ms，历史加载后「用时」不再消失
    _run_started_monotonic = time.monotonic()

    offset_counter = 0

    async def _emit(event: Dict[str, Any], *, terminal: bool = False) -> None:
        nonlocal offset_counter
        offset_counter = journal.allocate_event_offset(
            run_id,
            owner=None if terminal else owner,
            terminal=terminal,
        )
        await _xadd_event(run_id, offset_counter, event)

    async def _pause_for_tool_outcome(exc: BaseException) -> bool:
        logger.warning("chat_run_tool_outcome_unknown", run_id=run_id, effect_id=str(exc))
        if not journal.needs_attention(
            run_id,
            owner=owner,
            reason=f"tool outcome pending policy recovery: {exc}",
        ):
            current = get_run(run_id)
            if current is None or current.status != "needs_attention":
                logger.warning("chat_run_tool_unknown_cas_lost", run_id=run_id, owner=owner)
                return False
        await _emit(
            {
                "type": "error",
                "error": "工具调用结果无法安全确定，任务已暂停等待处理",
                "delta": "工具调用结果无法安全确定，任务已暂停等待处理",
                "chat_id": chat_id,
            },
            terminal=True,
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
        return True

    async def _pause_for_hook(exc: HookPaused) -> bool:
        logger.info("chat_run_hook_paused", run_id=run_id, reason=str(exc))
        if not journal.needs_attention(
            run_id,
            owner=owner,
            reason=f"hook requested pause: {exc}",
        ):
            current = get_run(run_id)
            if current is None or current.status != "needs_attention":
                logger.warning("chat_run_hook_pause_cas_lost", run_id=run_id, owner=owner)
                return False
        await _emit(
            {
                "type": "error",
                "error": "任务已按执行策略暂停，等待后续处理",
                "delta": "任务已按执行策略暂停，等待后续处理",
                "chat_id": chat_id,
            },
            terminal=True,
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
        return True

    from core.llm import workspace as _workspace_mod

    full_response = ""
    # A steer starts a new visible assistant segment in the same backend run.
    # Each segment gets its own message id so persisted history stays in the
    # same chronological order the user saw while streaming.
    current_message_id = message_id
    current_chat_seq = assistant_chat_seq
    latest_user_message = raw_user_message
    # Keep an exact prompt history for a queued follow-up. Mid-run steers add
    # their visible assistant/user segments here as they are durably committed.
    handoff_session_messages = _json_safe(list(session_messages or []))
    metadata: Dict[str, Any] = {}
    compaction_budget_inputs: Dict[str, Any] = {}
    tool_calls_log: list = []
    _workspace_mod.init_state()
    # 结构化 reasoning 通道（deepseek 系 reasoning_content/reasoning）的思考增量。
    # 落进独立的 thinking 列，不再拼进 full_response —— 拼进正文会把正文顶出
    # content 的 10 万字上限，截断切掉闭合标签后整段思考会漏成正文。
    # 每个思考块记录它出现时的正文偏移，历史重建按此原位还原。
    _thinking_parts: List[str] = []
    _thinking_log: List[Dict[str, Any]] = []
    # 正文 / 思考 / 工具卡片的先后，在它们产生的那一刻就记下来，落进
    # metadata.segments；刷新后照着渲染，不做任何反推。
    _segments = SegmentRecorder()
    # 本轮已开的思考块；跨轮（工具调用边界）置 None 强制另起一块。不设边界的话，
    # 正文一旦出现，后面每一轮的思考都会并进同一块里无限膨胀。
    _round_block: Optional[Dict[str, Any]] = None
    next_cancel_poll = 0.0

    def _flush_thinking() -> None:
        nonlocal _round_block
        if not _thinking_parts:
            return
        block = "".join(_thinking_parts)
        _thinking_parts.clear()
        # 结构化 reasoning 的尾部增量可能在正文已开始后才到达（正文首 token
        # 已出、思考收尾的"。"后到）。实时侧把它并回前一个思考块
        # （appendThinkingContentBeforeTrailingText），落库遵循同一规则——
        # 否则思考块会把正文句子从中间切开，刷新后与实时展示不一致。
        if _round_block is not None:
            _round_block["content"] += block
            return
        _round_block = {"content": block}
        _thinking_log.append(_round_block)
        _segments.add_thinking(len(_thinking_log) - 1)

    def _thinking_payload() -> Optional[List[Dict[str, Any]]]:
        blocks = [b for b in _thinking_log if b.get("content")]
        return blocks or None

    def _commit_cancelled_partial() -> None:
        """把用户按下停止那一刻已产出的正文与工具卡片落库。

        cancel_run 先写终态并清租约，之后 journal.complete 的 CAS 必然失败，
        worker 再也借不到 commit_effect 提交本轮输出 —— 所以这里直接写库。
        message_id 固定，用 upsert 保证重复取消 / 恢复不会留下两条。
        """
        _flush_thinking()
        if not (full_response or tool_calls_log):
            return
        # 停下来时还没回结果的工具：不标记的话前端按"缺 status 即成功"渲染，
        # 刷新后一张没跑完的卡片会显示成执行成功。
        for _tc in tool_calls_log:
            if "result" not in _tc and not _tc.get("status"):
                _tc["status"] = "interrupted"
        _ws_pinned = _workspace_mod.get_pinned()
        _extra = {
            "timestamp": now_iso(),
            "is_markdown": bool(
                "\n" in full_response or "```" in full_response or "**" in full_response
            ),
            "message_id": current_message_id,
            "run_id": run_id,
            "cancelled": True,
            "artifacts": _ws_pinned,
            "workspace_files": _workspace_mod.get_pinned_file_ids(),
            "duration_ms": int((time.monotonic() - _run_started_monotonic) * 1000),
            "segments": _segments.payload(),
        }
        if context.get("model_provider_id"):
            _extra["model_provider_id"] = context.get("model_provider_id")
        from core.services.project_scope import project_scope_from_context

        with SessionLocal() as db:
            chat_service = ChatService(db)
            chat_service.upsert_message(
                chat_id=chat_id,
                role="assistant",
                content=full_response,
                model=model_name,
                thinking=_thinking_payload(),
                tool_calls=tool_calls_log if tool_calls_log else None,
                message_id=current_message_id,
                chat_seq=current_chat_seq,
                extra_data=_extra,
                commit=False,
            )
            _persist_artifacts(
                db,
                user_id,
                chat_id,
                _ws_pinned,
                scope=project_scope_from_context(context),
                commit=False,
            )
            db.commit()

    def _persist_cancelled_partial() -> None:
        try:
            _commit_cancelled_partial()
        except Exception:  # noqa: BLE001 - 落库失败不能吞掉取消收尾
            logger.warning(
                "chat_run_cancelled_partial_persist_failed",
                run_id=run_id,
                exc_info=True,
            )

    try:
        if recovering:
            await _reset_stream_projection(run_id)
        # First frame: run_started — carries run_id / message_id; the frontend uses these to resume / cancel
        await _emit(
            {
                "type": "run_started",
                "run_id": run_id,
                "message_id": message_id,
                "chat_id": chat_id,
            }
        )
        if recovering:
            await _emit(
                {
                    "type": "content_replace",
                    "content": "",
                    "reason": "run_recovered",
                    "chat_id": chat_id,
                }
            )

        # Compaction notice (mirrors Codex's post-compaction Warning): after the
        # previous turn's stream closed, a new compaction checkpoint was written
        # in the background → notify the user once in this turn's first frame.
        # Failures are silent and must never affect the main conversation.
        try:
            from core.services.compaction_service import (
                get_compaction_context_state,
                pop_compaction_notice,
            )

            with SessionLocal() as _cn_db:
                _cn_service = ChatService(_cn_db)
                _notify_compaction = pop_compaction_notice(_cn_service, chat_id)
                _compaction_state = (
                    get_compaction_context_state(_cn_service, chat_id)
                    if _notify_compaction
                    else None
                )
            if _notify_compaction:
                await _emit(
                    {
                        "type": "compaction_notice",
                        "chat_id": chat_id,
                        "context_compaction": _compaction_state,
                    }
                )
        except Exception as _cn_exc:  # noqa: BLE001
            logger.debug("compaction_notice_failed", chat_id=chat_id, error=str(_cn_exc))

        # When the agent kicks off a batch_plan flow we want to suppress
        # follow-up question generation for THIS turn — the assistant's
        # message body is empty (just the batch_plan tool call), so the
        # follow-ups would either be nonsense or anchor to the user's
        # original prompt instead of the upcoming batch results.
        seen_batch_confirm = False

        # Evidence-plane join keys (GCE ticket 04). Injected here rather than at
        # every context construction site: this is the one place that owns both
        # the run and its pre-allocated assistant message id.
        context.setdefault("run_id", run_id)
        context.setdefault("message_id", message_id)
        context["journal_owner"] = owner

        # Stamp the message id onto tool logging for this run. The plumbing
        # already existed but had no caller, so every tool call was written with
        # an empty message_id — which silently broke the join the evidence plane
        # depends on, and left every episode with no tool sequence to learn from.
        try:
            from core.services.log_service import set_current_message_id

            set_current_message_id(message_id)
        except Exception:  # pragma: no cover - logging must never fail a run
            pass

        journal.append_operation(
            run_id,
            owner=owner,
            operation_type="model_dispatch",
            phase="model_inflight",
            safety="replayable",
        )
        async for chunk in _aiter_with_inactivity_timeout(
            astream_chat_workflow(
                session_messages=session_messages,
                user_message=effective_user_message,
                context=context,
            ),
            _INACTIVITY_TIMEOUT_SEC,
            is_activity=lambda item: _is_chat_run_activity(item, chat_id),
        ):
            now = time.monotonic()
            if now >= next_cancel_poll:
                next_cancel_poll = now + 0.25
                if is_run_cancelled(run_id):
                    raise asyncio.CancelledError
            chunk_type = chunk.get("type")

            if chunk_type == "thinking":
                _thinking_evt = build_thinking_event(chunk, chat_id)
                # 只累积真实思考增量；进度提示（message）与 structured_reasoning
                # 协议标记不落库
                if _thinking_evt.get("delta"):
                    _thinking_parts.append(str(_thinking_evt["delta"]))
                await _emit(_thinking_evt)

            elif chunk_type in {"ai_message", "content"}:
                delta = chunk.get("delta", "")
                if delta:
                    _flush_thinking()
                    full_response += delta
                    _segments.add_text(delta)
                    await _emit(
                        {
                            "type": "content",
                            "event": "ai_message",
                            "delta": delta,
                            "chat_id": chat_id,
                        }
                    )

            elif chunk_type == "content_replace":
                # Ontology review runs after the draft has streamed. If the
                # committee revised it, replace the visible/persisted answer
                # atomically instead of appending a second full answer.
                replacement = str(chunk.get("content") or "")
                _thinking_parts.clear()
                full_response = replacement
                _round_block = None
                _thinking_log.clear()
                # 整体替换后，先前记下的段落描述的是旧草稿，已无意义——重记。
                _segments.reset()
                _segments.add_text(replacement)
                await _emit(
                    {
                        "type": "content_replace",
                        "content": replacement,
                        "reason": chunk.get("reason"),
                        "chat_id": chat_id,
                    }
                )

            elif chunk_type == "steer_applied":
                _flush_thinking()
                steer_id = str(chunk.get("steer_id") or "")[:64]
                steer_queue_id = str(chunk.get("queue_id") or "")[:64]
                steer_claim_owner = str(chunk.get("claim_owner") or "")[:160]
                steer_seq = int(chunk.get("steer_seq") or 0)
                steer_message = str(chunk.get("message") or "").strip()
                steer_message_id = (
                    f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{run_id}:{steer_id}').hex[:16]}"
                    if steer_id
                    else f"msg_{uuid.uuid4().hex[:16]}"
                )
                next_assistant_message_id = (
                    f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{run_id}:{steer_id}:assistant').hex[:16]}"
                    if steer_id
                    else f"msg_{uuid.uuid4().hex[:16]}"
                )
                had_assistant_output = bool(full_response or tool_calls_log)
                if steer_message:
                    # Close the visible assistant segment before inserting the
                    # user's steer. Persisting in this order is what keeps a
                    # refresh from moving every mid-run user message above the
                    # whole assistant response.
                    def _commit_steer(db) -> None:
                        if steer_queue_id and steer_claim_owner:
                            from core.services.steer_queue import SteerQueue

                            owned_run = db.get(ChatRun, run_id)
                            SteerQueue.mark_applied_in_session(
                                db,
                                queue_id=steer_queue_id,
                                claim_owner=steer_claim_owner,
                                applied_run_id=run_id,
                                applied_operation_seq=int(owned_run.operation_seq or 0) + 1,
                            )
                        else:
                            owned_run = db.get(ChatRun, run_id)
                        chat_service = ChatService(db)
                        if had_assistant_output:
                            chat_service.add_message(
                                chat_id=chat_id,
                                role="assistant",
                                content=full_response,
                                model=model_name,
                                thinking=_thinking_payload(),
                                tool_calls=tool_calls_log if tool_calls_log else None,
                                message_id=current_message_id,
                                chat_seq=current_chat_seq,
                                extra_data={
                                    "timestamp": now_iso(),
                                    "is_markdown": bool(
                                        "\n" in full_response
                                        or "```" in full_response
                                        or "**" in full_response
                                    ),
                                    "message_id": current_message_id,
                                    "run_id": run_id,
                                    "steer_segment": True,
                                    "duration_ms": int(
                                        (time.monotonic() - _run_started_monotonic) * 1000
                                    ),
                                    "segments": _segments.payload(),
                                },
                                commit=False,
                            )
                        # Persist once the middleware has actually injected the
                        # instruction. A deterministic id makes replay safe.
                        chat_service.upsert_message(
                            chat_id=chat_id,
                            role="user",
                            content=steer_message,
                            message_id=steer_message_id,
                            extra_data={
                                "timestamp": now_iso(),
                                "steer": True,
                                "run_id": run_id,
                                "steer_id": steer_id,
                                "steer_queue_id": steer_queue_id or None,
                                "steer_seq": steer_seq or None,
                            },
                            commit=False,
                        )
                        # The durable queue acknowledgement and the exact
                        # post-steer prompt must share one transaction. If this
                        # worker disappears before the next model snapshot, a
                        # replacement worker resumes from this context instead
                        # of losing an already-applied instruction.
                        post_steer_messages = _json_safe(list(handoff_session_messages))
                        if had_assistant_output:
                            post_steer_messages.append(
                                {"role": "assistant", "content": full_response}
                            )
                        post_steer_messages.append({"role": "user", "content": steer_message})
                        post_steer_context = _json_safe(dict(context or {}))
                        post_steer_context.pop("journal_owner", None)
                        post_steer_context.update(
                            {
                                "run_id": run_id,
                                "message_id": next_assistant_message_id,
                                "chat_id": chat_id,
                                "user_id": user_id,
                            }
                        )
                        owned_run.recovery_snapshot = {
                            "kind": "chat",
                            "worker_args": {
                                "session_messages": post_steer_messages,
                                "effective_user_message": steer_message,
                                "raw_user_message": steer_message,
                                "context": post_steer_context,
                                "model_name": model_name,
                            },
                        }
                        owned_run.snapshot_version = int(owned_run.snapshot_version or 0) + 1

                    journal.append_operation(
                        run_id,
                        owner=owner,
                        operation_type="steer_messages_committed",
                        phase="steer_committed",
                        safety="side_effect_committed",
                        payload={
                            "steer_id": steer_id,
                            "queue_id": steer_queue_id or None,
                            "steer_seq": steer_seq or None,
                            "message_id": steer_message_id,
                            "previous_assistant_message_id": (
                                current_message_id if had_assistant_output else None
                            ),
                        },
                        commit_effect=_commit_steer,
                    )
                    if had_assistant_output:
                        handoff_session_messages.append(
                            {"role": "assistant", "content": full_response}
                        )
                    handoff_session_messages.append({"role": "user", "content": steer_message})
                    latest_user_message = steer_message
                await _emit(
                    {
                        "type": "steer_applied",
                        "chat_id": chat_id,
                        "run_id": run_id,
                        "steer_id": steer_id,
                        "queue_id": steer_queue_id or None,
                        "steer_seq": steer_seq or None,
                        "message": steer_message,
                        "message_id": steer_message_id,
                        "previous_assistant_message_id": (
                            current_message_id if had_assistant_output else None
                        ),
                        "next_assistant_message_id": next_assistant_message_id,
                        "had_assistant_output": had_assistant_output,
                    }
                )
                current_message_id = next_assistant_message_id
                current_chat_seq = None
                # The workflow reads this dict again when it emits the final
                # evolution/memory settlement marker, so keep that marker bound
                # to the post-steer assistant segment too.
                context["message_id"] = current_message_id
                full_response = ""
                tool_calls_log = []
                _thinking_parts.clear()
                _round_block = None
                _thinking_log.clear()
                _segments.reset()
                metadata = {}
                try:
                    from core.services.log_service import set_current_message_id

                    set_current_message_id(current_message_id)
                except Exception:  # pragma: no cover - logging must never fail a run
                    pass

            elif chunk_type == "tool_call":
                _flush_thinking()
                _round_block = None
                _tc_evt = build_tool_call_event(chunk, chat_id, tool_calls_log)
                _segments.add_tools(tool_calls_log)
                await _emit(_tc_evt)

            elif chunk_type == "tool_call_start":
                _flush_thinking()
                _round_block = None
                await _emit(build_tool_call_start_event(chunk, chat_id))

            elif chunk_type == "tool_call_delta":
                await _emit(build_tool_call_delta_event(chunk, chat_id))

            elif chunk_type == "tool_result":
                _tr_evt = build_tool_result_event(chunk, chat_id, tool_calls_log)
                # attach_tool_result 可能刚补录了一个没有 tool_call 事件的条目，
                # 此刻就把它排进段落表，否则它会缺席整条消息的展示顺序。
                _segments.add_tools(tool_calls_log)
                await _emit(_tr_evt)

            elif chunk_type == "context_usage":
                # Provider-reported usage is the authoritative live gauge.
                # Forward the sanitized snapshot unchanged; unlike subagent
                # subSteps, every token represented here reached the primary
                # model wire.
                await _emit({**chunk, "chat_id": chat_id})

            elif chunk_type in ("heartbeat", "model_progress"):
                # Neither is written to the stream. heartbeat = transport
                # keep-alive (excluded from is_activity); model_progress = the
                # model is still streaming (e.g. long tool-call-arg generation)
                # with nothing mapping to an SSE event — merely receiving it
                # resets the inactivity watchdog.
                continue

            elif chunk_type == "tool_pending":
                pending_event = {
                    "type": "tool_pending",
                    "chat_id": chat_id,
                    "reason": chunk.get("reason", "llm_buffering"),
                }
                if chunk.get("scope"):
                    pending_event["scope"] = chunk["scope"]
                await _emit(pending_event)

            elif chunk_type == "subagent_event":
                # Streaming sub-steps inside a subagent — passed through as-is
                # (including parent_tool_id/sub_run_id/sub_type and their own
                # fields); the frontend renders them under the call_subagent
                # card. The chunk already carries type="subagent_event", so we
                # just add chat_id in place (consumed once, safe to mutate).
                # First accumulate into the call_subagent entry in
                # tool_calls_log → persisted, so it can be replayed after a refresh.
                try:
                    attach_subagent_step(
                        tool_calls_log,
                        str(chunk.get("parent_tool_id", "") or ""),
                        chunk,
                    )
                except Exception:
                    logger.debug("attach_subagent_step failed (ignored)", exc_info=True)
                chunk["chat_id"] = chat_id
                # 落库（attach_subagent_step）已在上面完成，推给浏览器的这一份走展示上限：
                # 子智能体的工具结果和主链路一样能有几 MB。
                await _emit(bound_result_for_display(chunk)[0])

            elif chunk_type == "file_confirm":
                # §13: some tool coroutine is suspended, waiting for the user to
                # confirm a write to "My Space". Forward to the frontend to show
                # the confirmation bar; this SSE stream does NOT end — after the
                # user's out-of-band POST /v1/chats/{id}/file-confirm the
                # suspended tool resumes in place, and subsequent
                # tool_result / meta events keep flowing on this same stream.
                await _emit(
                    {
                        "type": "file_confirm",
                        "chat_id": chat_id,
                        "confirm_id": chunk.get("confirm_id"),
                        "op": chunk.get("op"),
                        "logical_path": chunk.get("logical_path"),
                        "message": chunk.get("message"),
                        # §13 timeout-reclaim signal: when True the frontend dismisses the zombie confirmation bar instead of showing it
                        "expired": chunk.get("expired", False),
                    }
                )

            elif chunk_type == "design_pick":
                # Site-builder design pick (choose one of three): the choose_design
                # tool coroutine is suspended waiting for the user's pick.
                # Same mechanism as file_confirm, but the payload additionally has
                # question/options (the file_confirm branch copies a whitelist of
                # fields, hence the separate pass-through branch here).
                await _emit(
                    {
                        "type": "design_pick",
                        "chat_id": chat_id,
                        "confirm_id": chunk.get("confirm_id"),
                        "question": chunk.get("question", ""),
                        "options": chunk.get("options", []),
                        "message": chunk.get("message"),
                        "expired": chunk.get("expired", False),
                    }
                )

            elif chunk_type == "user_question":
                await _emit(build_user_question_event(chunk, chat_id))

            elif chunk_type == "user_question_resolved":
                await _emit(build_user_question_resolved_event(chunk, chat_id))

            elif chunk_type == "batch_confirm":
                # Forward the batch-execution confirmation event to the frontend —
                # triggered by the batch_runner MCP tool; workflow.py has already
                # broken out of the current agent loop. After the user confirms in
                # the dialog, the frontend calls POST /v1/batch/{id}/confirm and
                # opens a separate SSE stream.
                seen_batch_confirm = True
                await _emit(
                    {
                        "type": "batch_confirm",
                        "chat_id": chat_id,
                        "plan_id": chunk.get("plan_id"),
                        "total": chunk.get("total"),
                        "preview": chunk.get("preview", []),
                        "default_template": chunk.get("default_template", ""),
                        "placeholder_keys": chunk.get("placeholder_keys", []),
                        "source_type": chunk.get("source_type"),
                        "warnings": chunk.get("warnings", []),
                    }
                )

            elif chunk_type == "plan_update":
                # The main agent updated its lightweight plan checklist via the
                # update_plan tool. Forward full-state to the frontend, which
                # renders it as a plan bar above the chat input (not in the
                # message flow). The agent keeps executing in the same turn —
                # no approval gate, no loop abort.
                await _emit(
                    {
                        "type": "plan_update",
                        "chat_id": chat_id,
                        "title": chunk.get("title", ""),
                        "steps": chunk.get("steps", []),
                    }
                )

            elif chunk_type in {
                "ontology_activation",
                "ontology_gate",
                "ontology_review",
                "ontology_repair",
                "ontology_revision",
                "ontology_revision_thinking",
            }:
                ontology_event = dict(chunk)
                ontology_event["chat_id"] = chat_id
                await _emit(bound_result_for_display(ontology_event)[0])

            elif chunk_type == "meta":
                _flush_thinking()
                internal_budget = chunk.get("_compaction_budget_inputs")
                if isinstance(internal_budget, dict):
                    compaction_budget_inputs = dict(internal_budget)
                # Strict workspace gate: pinned list is the sole source of
                # user-visible artifacts. See chats.py:_stream_sse_response.
                _ws_pinned = _workspace_mod.get_pinned()
                _ws_files = _workspace_mod.get_pinned_file_ids()
                usage_payload = chunk.get("usage") or None
                context_usage_payload = chunk.get("context_usage") or None
                compaction_pending = _should_schedule_compaction(
                    model_name=model_name,
                    usage=usage_payload,
                )
                # 本轮总耗时：同一个值既下发给正在看的这一页，也写进 extra_data 供历史
                # 加载使用。以前只写库不下发，前端只能自己按「占位气泡创建到现在」估一个，
                # 把网络往返和排队也算了进去 —— 于是同一条回答，实时看到的耗时和刷新后
                # 从历史读出来的对不上。
                _duration_ms = int((time.monotonic() - _run_started_monotonic) * 1000)
                metadata = {
                    "type": "meta",
                    "duration_ms": _duration_ms,
                    "route": chunk.get("route", "main"),
                    "sources": chunk.get("sources", []),
                    "artifacts": _ws_pinned,
                    "warnings": chunk.get("warnings", []),
                    "is_markdown": chunk.get("is_markdown", False),
                    "chat_id": chat_id,
                    "message_id": current_message_id,
                    "citations": chunk.get("citations", []),
                    "workspace_files": _ws_files,
                    "context_usage": context_usage_payload,
                    "compaction_pending": compaction_pending,
                    "ontology_governance": chunk.get("ontology_governance"),
                    # Skeleton marker: settlement runs after the stream closes.
                    "evolution_pending": chunk.get("evolution_pending"),
                }
                await _emit(metadata)

                # Persist: assistant message + artifacts
                _persist_extra = {
                    "timestamp": now_iso(),
                    "route": metadata.get("route"),
                    "is_markdown": metadata.get("is_markdown", False),
                    "sources": metadata.get("sources", []),
                    "artifacts": metadata.get("artifacts", []),
                    "warnings": metadata.get("warnings", []),
                    "citations": metadata.get("citations", []),
                    "message_id": current_message_id,
                    "workspace_files": _ws_files,
                    "duration_ms": _duration_ms,
                    "segments": _segments.payload(),
                }
                if isinstance(context_usage_payload, dict):
                    _persist_extra["context_usage"] = context_usage_payload
                if metadata.get("ontology_governance"):
                    _persist_extra["ontology_governance"] = metadata["ontology_governance"]
                if context.get("model_provider_id"):
                    _persist_extra["model_provider_id"] = context.get("model_provider_id")
                journal.save_snapshot(
                    run_id,
                    owner=owner,
                    phase="model_completed",
                    safety="replayable",
                    snapshot=_json_safe(
                        {
                            "assistant_content": full_response,
                            "message_id": current_message_id,
                            "model_name": model_name,
                            "thinking": _thinking_payload(),
                            "tool_calls": tool_calls_log if tool_calls_log else None,
                            "usage": usage_payload,
                            "extra_data": _persist_extra,
                            "artifacts": _ws_pinned,
                            "context": context,
                        }
                    ),
                )

                queued_handoff: Dict[str, Any] = {}

                def _commit_output(db) -> None:
                    chat_service = ChatService(db)
                    chat_service.add_message(
                        chat_id=chat_id,
                        role="assistant",
                        content=full_response,
                        model=model_name,
                        thinking=_thinking_payload(),
                        tool_calls=tool_calls_log if tool_calls_log else None,
                        usage=usage_payload,
                        message_id=current_message_id,
                        chat_seq=current_chat_seq,
                        extra_data=_persist_extra,
                        commit=False,
                    )
                    # Build a ProjectScope from the workflow context and pass it
                    # explicitly so pinned files keep their project ownership.
                    from core.services.project_scope import project_scope_from_context

                    _persist_artifacts(
                        db,
                        user_id,
                        chat_id,
                        _ws_pinned,
                        scope=project_scope_from_context(context),
                        commit=False,
                    )
                    prepared = _commit_queued_handoff_in_session(
                        db,
                        source_run_id=run_id,
                        chat_id=chat_id,
                        user_id=user_id,
                        session_messages=handoff_session_messages,
                        assistant_content=full_response,
                        context=context,
                        model_name=model_name,
                    )
                    if prepared is not None:
                        queued_handoff.update(prepared)

                # Offloaded to a worker thread on purpose. This is the one
                # journal call that commits the turn's real side effects (the
                # assistant message, pinned artifacts, a queued handoff), so it
                # is the one that can sit waiting on a row lock. ``complete``
                # is synchronous SQLAlchemy; calling it inline blocks the event
                # loop, and a blocked loop cannot run the coroutine holding the
                # conflicting transaction — the process deadlocks and every
                # request, stream and health probe stops answering. In a thread
                # a lock wait costs this run its latency and nothing else.
                # ``_commit_output`` and everything it calls are plain sync
                # code, so they are safe to run off the loop.
                completed = await asyncio.to_thread(
                    functools.partial(
                        journal.complete,
                        run_id,
                        owner=owner,
                        status="completed",
                        usage=usage_payload,
                        last_event_offset=offset_counter,
                        commit_effect=_commit_output,
                        committed_operation={
                            "operation_type": "message_committed",
                            "phase": "message_committed",
                            "safety": "side_effect_committed",
                            "payload": {"message_id": current_message_id},
                        },
                    )
                )
                if not completed:
                    logger.warning(
                        "chat_run_terminal_cas_lost",
                        run_id=run_id,
                        owner=owner,
                    )
                    return

                # The DB lease has intentionally ended. Stop the heartbeat
                # before scheduling the handoff and projecting terminal events;
                # otherwise its next renew would interpret normal completion as
                # lease loss and cancel this coroutine mid-handoff.
                lease_task.cancel()

                if queued_handoff:
                    _schedule_queued_handoff(
                        queued_handoff,
                        chat_id=chat_id,
                        user_id=user_id,
                    )
                    await _emit(
                        _queued_run_started_event(run_id, chat_id, queued_handoff),
                        terminal=True,
                    )

                # Generate follow-ups in the background (same as the original
                # behavior — doesn't block stream close). Skipped in the batch
                # execution scenario though: the assistant message is empty
                # (just the batch_plan tool call), and the real answers come in
                # the per-item batch results. The right time to regenerate
                # follow-ups is when the batch finishes, on the final entry, by
                # the frontend — so skip here to avoid popping the question list
                # at the wrong moment.
                if not seen_batch_confirm:
                    _spawn_followup_task(
                        run_id=run_id,
                        chat_id=chat_id,
                        user_msg=latest_user_message,
                        response=full_response,
                        msg_id=current_message_id,
                    )

                # End-of-turn compaction: when real token usage crosses the
                # threshold → generate a summary checkpoint in the background;
                # doesn't block stream close, and failures don't affect the main
                # conversation.
                _spawn_compaction_task(
                    run_id=run_id,
                    chat_id=chat_id,
                    model_name=model_name,
                    usage=usage_payload,
                    budget_inputs=compaction_budget_inputs,
                    should_schedule=compaction_pending,
                )

        # AgentScope may absorb a tool adapter exception after ToolEffectMiddleware
        # has already durably paused the run. Re-read the DB before projecting a
        # normal terminal marker; otherwise allocate_event_offset correctly
        # rejects the non-completed run and the outer handler overwrites
        # ``needs_attention`` with ``failed``.
        try:
            current = get_run(run_id)
        except AttributeError:
            # Some narrow unit adapters replace SessionLocal with a commit-only
            # fake that intentionally has no query surface.
            current = None
        if current is not None:
            if current.status == "needs_attention":
                await _pause_for_tool_outcome(
                    RuntimeError(current.failure_reason or "tool outcome requires recovery")
                )
                return
            if current.status in _TERMINAL_STATUSES and current.status != "completed":
                raise asyncio.CancelledError

        # Workflow returned normally (with or without a meta event): write the terminal marker
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)

    except asyncio.CancelledError:
        if lease_lost.is_set():
            # Another process owns recovery now. The fenced worker must not
            # project a user cancellation or mutate the durable row. The one
            # exception is fencing caused by the user's own cancel: nobody will
            # take that run over, so the partial answer must be persisted here.
            logger.warning("chat_run_worker_fenced", run_id=run_id, owner=owner)
            if _cancelled_by_user(run_id):
                _persist_cancelled_partial()
        else:
            current = get_run(run_id)
            if current is None or current.status != "cancelled":
                # Reaper/hard-cap/external recovery may cancel the local task
                # after publishing its own authoritative terminal verdict.
                logger.warning(
                    "chat_run_worker_stopped_by_external_terminal",
                    run_id=run_id,
                    status=(current.status if current is not None else None),
                )
            else:
                # Triggered by cancel_run: persist what was already produced,
                # then write a user-cancelled event + terminal marker so
                # followers exit gracefully.
                logger.info("chat_run_cancelled", run_id=run_id)
                _persist_cancelled_partial()
                await _emit(
                    {
                        "type": "error",
                        "error": "任务已被用户取消",
                        "delta": "任务已被用户取消",
                        "chat_id": chat_id,
                        "_cancelled": True,
                    },
                    terminal=True,
                )
                await _emit(
                    {"type": _TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True},
                    terminal=True,
                )

    except ToolOutcomeUnknown as exc:
        await _pause_for_tool_outcome(exc)

    except HookPaused as exc:
        await _pause_for_hook(exc)

    except RunLeaseLost:
        # A takeover/cancel may race with any journal append. Once fenced, the
        # old worker must not persist fallback messages or publish a terminal —
        # except when the fencing came from the user's own cancel.
        logger.warning("chat_run_worker_fenced", run_id=run_id, owner=owner)
        if _cancelled_by_user(run_id):
            _persist_cancelled_partial()

    except Exception as exc:
        nested_pause = find_hook_paused(exc)
        if nested_pause is not None:
            await _pause_for_hook(nested_pause)
            return
        nested_unknown = find_tool_outcome_unknown(exc)
        if nested_unknown is not None:
            await _pause_for_tool_outcome(nested_unknown)
            return
        logger.error("chat_run_failed", run_id=run_id, error=str(exc), exc_info=True)
        try:
            user_facing = resolve_user_facing_error(exc)
        except Exception:
            user_facing = "请求处理失败，请稍后重试"
        failed = journal.complete(
            run_id,
            owner=owner,
            status="failed",
            failure_reason=str(exc)[:1000],
            last_event_offset=offset_counter,
        )
        if not failed:
            logger.warning(
                "chat_run_failure_cas_lost",
                run_id=run_id,
                owner=owner,
            )
            return
        # Preserve the historical empty error placeholder, but only after this
        # owner wins the failed terminal CAS. A cancelled/taken-over worker
        # must never leave a late message behind.
        try:
            with SessionLocal() as db:
                ChatService(db).add_message(
                    chat_id=chat_id,
                    role="assistant",
                    content="",
                    model=model_name,
                    message_id=current_message_id,
                    chat_seq=current_chat_seq,
                    error={"error": str(exc), "timestamp": _utcnow().isoformat()},
                )
        except Exception:
            pass
        await _emit(
            {
                "type": "error",
                "error": user_facing,
                "delta": user_facing,
                "chat_id": chat_id,
            },
            terminal=True,
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)

    finally:
        # After the terminal state, keep the event stream for the full human
        # wait window plus a recovery grace period so late reconnects can replay.
        await _expire_stream(run_id)
        lease_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await lease_task
        _acknowledge_terminal_writer(run_id)


def _should_schedule_compaction(
    *,
    model_name: str,
    usage: Optional[Dict] = None,
) -> bool:
    """Return whether the post-turn compactor will be scheduled."""
    try:
        from core.config.settings import settings as _settings

        if not _settings.compaction.enabled:
            return False
        from core.llm.context_manager import resolve_model_context_window
        from core.services.compaction_service import (
            resolve_active_tokens,
            resolve_token_limit,
            resolve_trigger_ratio,
            should_compact,
        )

        active_tokens = resolve_active_tokens(usage)
        limit = resolve_token_limit(
            resolve_model_context_window(model_name or ""),
            ratio=resolve_trigger_ratio(),
        )
        return should_compact(active_tokens, limit)
    except Exception:
        logger.debug("compaction_schedule_check_failed", exc_info=True)
        return False


def _spawn_compaction_task(
    *,
    run_id: str,
    chat_id: str,
    model_name: str,
    usage: Optional[Dict] = None,
    budget_inputs: Optional[Dict[str, Any]] = None,
    should_schedule: Optional[bool] = None,
) -> None:
    """PostTurn phase: background pre-warm of the shared compaction engine.

    Fire-and-forget; never affects the main conversation. This is *not* a second
    compactor — it calls the same
    :func:`core.services.compaction_service.run_compaction` the mid-turn hook
    does, and normally finds nothing to do: a turn that crossed the threshold
    while it still had steps left has already compacted mid-turn and persisted
    its checkpoint. It earns its keep only when the threshold is crossed by a
    turn's *final* reasoning step, where there is no later step boundary to
    catch it; doing it here rather than at the next turn's PreTurn check keeps
    the summarisation off the user-visible latency path.

    ``run_id`` and ``budget_inputs`` are threaded through to the Coordinator so
    the checkpoint is published under the run's hook bus and usage ledger, and
    so its component estimator reuses the turn's already-frozen execution
    surface instead of re-deriving one.
    """
    schedule = (
        _should_schedule_compaction(model_name=model_name, usage=usage)
        if should_schedule is None
        else should_schedule
    )
    if not schedule:
        return

    async def _bg() -> None:
        try:
            from core.llm.context_manager import resolve_model_context_window
            from core.services.compaction_service import (
                resolve_token_limit,
                resolve_trigger_ratio,
                run_post_turn_compaction,
            )

            # Trigger criterion — see resolve_active_tokens: real end-of-turn
            # context occupancy, not the cumulative billing value. Gating here
            # keeps a turn that never approached the window from taking the
            # compaction lease at all; the Coordinator re-checks against the
            # authoritative snapshot estimate before calling the summariser.
            limit = resolve_token_limit(
                resolve_model_context_window(model_name or ""), ratio=resolve_trigger_ratio()
            )
            logger.info(
                "chat_compaction_evaluating",
                chat_id=chat_id,
                limit=limit,
            )
            await run_post_turn_compaction(
                chat_id,
                budget_inputs=dict(budget_inputs or {}),
                token_limit=limit,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("background_compaction_failed", error=str(exc))

    try:
        asyncio.create_task(_bg())
    except RuntimeError:
        pass


def _spawn_followup_task(
    *, run_id: str, chat_id: str, user_msg: str, response: str, msg_id: str
) -> None:
    """Equivalent to the existing _generate_followups_bg in chats.py."""
    from core.llm.message_compat import strip_thinking
    from orchestration.followups import get_followup_generator

    async def _bg() -> None:
        try:
            clean_resp = strip_thinking(response)
            questions = await asyncio.wait_for(
                get_followup_generator().generate(user_msg, clean_resp, run_id=run_id),
                timeout=10,
            )
            if questions:
                with SessionLocal() as bg_db:
                    ChatService(bg_db).update_message_extra_data(
                        msg_id, {"follow_up_questions": questions}
                    )
        except Exception as exc:
            logger.warning("background_followup_failed", error=str(exc))

    asyncio.create_task(_bg())


# ─── Follower: read events from the Redis Stream ───────────────────────


async def follow_run(run_id: str, *, from_offset: int = 0) -> AsyncIterator[Dict[str, Any]]:
    """Read events from the Redis Stream, delivering those after ``from_offset``.

    Flow:
    1. XRANGE - + fetches existing events in one shot; deliver the ones with ``_offset > from_offset``
    2. XREAD STREAMS key {last_id} BLOCK 5000 blocks waiting for new events
    3. Stop when a ``__terminal__``-typed event is received
    4. Block timeout + run in a terminal state → exit gracefully (backstop so the follower never hangs forever)

    Uses the dedicated stream pool: the blocking read below holds its
    connection for the whole BLOCK window, so followers must not draw from the
    connections that ordinary API requests need.
    """
    redis = get_redis(blocking=True)
    key = _stream_key(run_id)

    run = get_run(run_id)
    if run is None:
        # The chats.py route layer should have validated this already; this is a second line of defense
        raise ChatRunNotFound(f"chat run {run_id} not found")

    last_id = "0-0"
    terminal_seen = False

    # ── Phase 1: replay historical events ──
    try:
        history = await redis.xrange(key, min="-", max="+", count=None)
    except Exception as exc:
        logger.warning("chat_run_xrange_failed", run_id=run_id, error=str(exc))
        history = []

    for entry_id, fields in history:
        last_id = entry_id
        event = _decode_entry(fields)
        if event is None:
            continue
        if event.get("type") == _TERMINAL_TYPE:
            terminal_seen = True
            break
        if event.get("_offset", 0) > from_offset:
            yield event

    if terminal_seen:
        return

    # ── Phase 2: blocking tail ──
    while True:
        try:
            result = await redis.xread({key: last_id}, count=100, block=_XREAD_BLOCK_MS)
        except RedisTimeoutError:
            # Benign: the BLOCK window elapsed with no new events — semantically
            # identical to an empty result. (redis-py 8.0 defaults socket_timeout
            # to 5s; if it ever equals _XREAD_BLOCK_MS the read raises here on
            # every idle window instead of returning nil. We size socket_timeout
            # well above the block in core/infra/redis.py, but treat the timeout
            # as "no events" regardless so a long, quiet run never spams logs.)
            result = None
        except Exception as exc:
            logger.warning("chat_run_xread_failed", run_id=run_id, error=str(exc))
            await asyncio.sleep(0.5)
            result = None

        if not result:
            # Block timed out: check whether the run is terminal + whether the stream has new events
            current = get_run(run_id)
            if current is not None and current.status in _TERMINAL_STATUSES:
                # After the terminal state, try reading the latest events once more (covers the race)
                try:
                    tail = await redis.xrange(key, min=_next_id(last_id), max="+", count=200)
                except Exception:
                    tail = []
                for entry_id, fields in tail:
                    last_id = entry_id
                    event = _decode_entry(fields)
                    if event is None:
                        continue
                    if event.get("type") == _TERMINAL_TYPE:
                        return
                    if event.get("_offset", 0) > from_offset:
                        yield event
                return
            continue

        for _stream_name, entries in result:
            for entry_id, fields in entries:
                last_id = entry_id
                event = _decode_entry(fields)
                if event is None:
                    continue
                if event.get("type") == _TERMINAL_TYPE:
                    return
                if event.get("_offset", 0) > from_offset:
                    yield event


def _decode_entry(fields: Any) -> Optional[Dict[str, Any]]:
    """Decode the fields of a single Redis Stream entry into an SSE event dict.

    Returns None for undecodable entries and for internal liveness markers
    (``model_progress``) — nothing writes them to streams anymore (the reaper
    now trusts in-process task liveness), but entries from older builds may
    survive within the stream TTL and must never reach clients (the frontend
    treats unknown event types as "pending ended").
    Only used by follow_run_as_sse's replay/tail paths.
    """
    if not fields:
        return None
    raw = fields.get("data") if isinstance(fields, dict) else None
    if raw is None:
        return None
    try:
        event = json.loads(raw)
    except Exception:
        return None
    if isinstance(event, dict) and event.get("type") == "model_progress":
        return None
    return event


def _next_id(last_id: str) -> str:
    """Given a Redis stream id (``ts-seq``), return the next minimal id usable for XRANGE."""
    if "-" not in last_id:
        return last_id
    ts, seq = last_id.split("-", 1)
    try:
        return f"{ts}-{int(seq) + 1}"
    except ValueError:
        return last_id


def get_run(run_id: str) -> Optional[ChatRun]:
    """Read a single run (used by the route layer for authorization)."""
    with SessionLocal() as db:
        return db.query(ChatRun).filter(ChatRun.run_id == run_id).first()


def is_run_cancelled(run_id: str) -> bool:
    """Polled by plan_mode every ~15s heartbeat; see ``cancel_run`` for why.

    Any terminal state stops the worker (not just ``cancelled``): a run reaped
    to ``failed`` by the stale reaper must also make the cooperative worker stop
    itself as soon as possible, or we get the split-brain of "DB says dead, task
    keeps running". The worker itself only writes the terminal state during
    wrap-up, so any terminal state seen while polling must come from an external
    verdict.
    """
    with SessionLocal() as db:
        row = db.query(ChatRun.status).filter(ChatRun.run_id == run_id).first()
        return bool(row and row[0] in _TERMINAL_STATUSES)


# ─── Plan-execute mode: reuses the ChatRun table + Redis Stream + cancel + orphan handling ──


async def start_plan_execute_run(
    *,
    plan_id: str,
    chat_id: str,
    user_id: str,
    enabled_mcp_ids: Optional[List[str]] = None,
    enabled_skill_ids: Optional[List[str]] = None,
    enabled_kb_ids: Optional[List[str]] = None,
    enabled_agent_ids: Optional[List[str]] = None,
    session_messages: List[Dict[str, Any]],
    model_name: Optional[str] = None,
) -> ChatRun:
    """Plan Phase-2 (execute): background task on the same ChatRun infrastructure.

    chat_id must exist in chat_sessions (FK). Caller should run _ensure_plan_session first.
    """
    if not chat_id:
        raise ValueError("chat_id is required to start a plan execute run")

    request_payload = {
        "kind": "plan_execute",
        "plan_id": plan_id,
        "chat_id": chat_id,
        "enabled_mcp_ids": enabled_mcp_ids or [],
        "enabled_skill_ids": enabled_skill_ids or [],
        "enabled_kb_ids": enabled_kb_ids or [],
        "enabled_agent_ids": enabled_agent_ids or [],
        "model_name": model_name,
    }
    effective_model_name = model_name or DEFAULT_CHAT_MODEL_ALIAS
    run = _create_run_record(
        chat_id=chat_id,
        user_id=user_id,
        request_payload=request_payload,
        recovery_snapshot={
            "kind": "plan_execute",
            "worker_args": {
                "context": {
                    "mcp_ids": list(enabled_mcp_ids or []),
                    "skill_ids": list(enabled_skill_ids or []),
                    "kb_ids": list(enabled_kb_ids or []),
                    "model_name": effective_model_name,
                }
            },
        },
    )
    journal_owner = _new_worker_owner(run.run_id)
    _register_run_task(
        run.run_id,
        _run_plan_execute_workflow(
            run_id=run.run_id,
            plan_id=plan_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=run.message_id,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_skill_ids=enabled_skill_ids,
            enabled_kb_ids=enabled_kb_ids,
            enabled_agent_ids=enabled_agent_ids,
            session_messages=session_messages,
            model_name=effective_model_name,
            journal_owner=journal_owner,
        ),
        name=f"plan_run:{run.run_id}",
    )
    logger.info(
        "plan_run_started",
        run_id=run.run_id,
        plan_id=plan_id,
        chat_id=chat_id,
        user_id=user_id,
    )
    return run


async def _run_plan_execute_workflow(
    *,
    run_id: str,
    plan_id: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    enabled_mcp_ids: Optional[List[str]],
    enabled_skill_ids: Optional[List[str]],
    enabled_kb_ids: Optional[List[str]],
    enabled_agent_ids: Optional[List[str]],
    session_messages: List[Dict[str, Any]],
    model_name: str,
    journal_owner: Optional[str] = None,
) -> None:
    """Background plan-execute worker. Streams via XADD, persists at end.

    Uses short-lived sessions (one per logical operation) to avoid holding a
    DB connection across the entire long-running stream.
    """
    from core.chat.tool_log import attach_tool_result as _attach_tool_result
    from core.llm import workspace as _workspace_mod
    from core.services.artifact_service import persist_artifacts as _persist_artifacts
    from core.services.plan_service import PlanService
    from orchestration.subagents.plan_mode import astream_execute_plan

    owner = journal_owner or _new_worker_owner(run_id)
    journal = _journal()
    if not journal.claim(run_id, owner=owner, lease_seconds=_RUN_LEASE_SECONDS):
        logger.info("plan_run_claim_skipped", run_id=run_id, owner=owner)
        return
    current = asyncio.current_task()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _lease_heartbeat(run_id, owner, current, lease_lost),
        name=f"plan_run_lease:{run_id}",
    )

    offset_counter = 0

    async def _emit(event: Dict[str, Any], *, terminal: bool = False) -> None:
        nonlocal offset_counter
        offset_counter = journal.allocate_event_offset(
            run_id,
            owner=None if terminal else owner,
            terminal=terminal,
        )
        await _xadd_event(run_id, offset_counter, event)

    async def _pause_plan_tool_outcome(exc: BaseException) -> None:
        if not journal.needs_attention(
            run_id,
            owner=owner,
            reason=f"tool outcome pending policy recovery: {exc}",
        ):
            return
        await _emit(
            {
                "type": "plan_error",
                "plan_id": plan_id,
                "error": "工具调用结果无法安全确定，任务已暂停等待处理",
            },
            terminal=True,
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)

    result_text = ""
    completed_steps = 0
    total_steps = 0
    exec_usage: Optional[Dict[str, Any]] = None
    tool_calls_log: List[Dict[str, Any]] = []
    # Strict workspace gate also applies to plan-execute runs. The plan
    # subagent has access to pin_to_workspace and is expected to pin its
    # final deliverables.
    _workspace_mod.init_state()

    try:
        await _emit(
            {
                "type": "run_started",
                "run_id": run_id,
                "message_id": message_id,
                "chat_id": chat_id,
                "kind": "plan_execute",
                "plan_id": plan_id,
            }
        )

        # astream_execute_plan needs a Session; isolate it from the persistence one.
        from core.llm.middlewares import CURRENT_RUN_BINDING

        binding_token = CURRENT_RUN_BINDING.set((run_id, owner))
        try:
            with SessionLocal() as stream_db:
                async for event in astream_execute_plan(
                    plan_id=plan_id,
                    user_id=user_id,
                    db=stream_db,
                    model_name=model_name,
                    enabled_mcp_ids=enabled_mcp_ids,
                    enabled_skill_ids=enabled_skill_ids,
                    enabled_kb_ids=enabled_kb_ids,
                    enabled_agent_ids=enabled_agent_ids,
                    session_messages=session_messages,
                    chat_id=chat_id,
                    run_id=run_id,
                ):
                    evt_type = event.get("type")
                    if evt_type == "plan_complete":
                        result_text = event.get("result_text", "")
                        completed_steps = event.get("completed_steps", 0)
                        total_steps = event.get("total_steps", 0)
                        exec_usage = event.get("usage") or None
                    elif evt_type == "tool_call":
                        tool_calls_log.append(
                            {
                                "tool_name": event.get("tool_name"),
                                "tool_id": event.get("tool_id"),
                                "tool_args": event.get("tool_args", {}),
                                "step_id": event.get("step_id"),
                            }
                        )
                    elif evt_type == "tool_result":
                        res = event.get("result")
                        tid = event.get("tool_id")
                        tn = event.get("tool_name")
                        _attach_tool_result(tool_calls_log, tid, tn, res)
                    await _emit(event)
        finally:
            CURRENT_RUN_BINDING.reset(binding_token)

        # Build plan snapshot in its own short session
        plan_snapshot = None
        try:
            with SessionLocal() as snap_db:
                updated_plan = PlanService(snap_db).get_plan(plan_id, user_id)
                if updated_plan:
                    plan_snapshot = PlanService.build_execution_snapshot(
                        updated_plan,
                        completed_steps=completed_steps,
                        total_steps=total_steps,
                        result_text=result_text,
                    )
        except Exception as snap_exc:
            logger.warning("plan_run_snapshot_failed", run_id=run_id, error=str(snap_exc))

        # Strict workspace gate: only files pinned via pin_to_workspace
        # surface anywhere user-visible — both the chat message and the
        # user's file library ("My Space") are sourced from this list.
        _ws_pinned = _workspace_mod.get_pinned()
        artifacts_meta = _ws_pinned
        _ws_files = _workspace_mod.get_pinned_file_ids()

        def _commit_plan_result(persist_db) -> None:  # noqa: ANN001
            chat_service = ChatService(persist_db)
            # 被用户中断的那一轮同样会走到这里（协作式取消让生成器正常收尾），
            # 照着"执行完成"落库会让用户回到会话时看到一句"计划执行完成"，
            # 与他刚按下的停止完全对不上。按快照里的中断位分开措辞。
            _cancelled = bool((plan_snapshot or {}).get("cancelled"))
            content = result_text or (
                f"计划执行已中断：共 {total_steps} 步，完成 {completed_steps} 步。"
                if _cancelled
                else f"计划执行完成：共 {total_steps} 步，完成 {completed_steps} 步。"
            )
            chat_service.add_message(
                chat_id=chat_id,
                role="assistant",
                content=content,
                model=model_name,
                message_id=message_id,
                extra_data={
                    "is_markdown": bool(result_text),
                    "plan_id": plan_id,
                    "completed_steps": completed_steps,
                    "total_steps": total_steps,
                    "plan_snapshot": plan_snapshot,
                    "artifacts": artifacts_meta,
                    "workspace_files": _ws_files,
                    "message_id": message_id,
                },
                tool_calls=tool_calls_log or None,
                usage=exec_usage,
                commit=False,
            )
            # The plan-execute background worker doesn't hold a workflow context
            # dict, so it builds the ProjectScope via the reverse-lookup path
            # chat_id → ChatSession → Project.
            from core.services.project_scope import project_scope_from_chat_id

            _persist_artifacts(
                persist_db,
                user_id,
                chat_id,
                _ws_pinned,
                scope=project_scope_from_chat_id(persist_db, chat_id),
                commit=False,
            )

        def _persist_plan_partial() -> None:
            # _commit_plan_result 走 add_message（非幂等），重复写会撞主键
            try:
                with SessionLocal() as _db:
                    if _db.get(ChatMessage, message_id) is not None:
                        return
                    _commit_plan_result(_db)
                    _db.commit()
            except Exception:  # noqa: BLE001 - 落库失败不能吞掉取消收尾
                logger.warning(
                    "plan_run_cancelled_partial_persist_failed",
                    run_id=run_id,
                    exc_info=True,
                )

        completed = journal.complete(
            run_id,
            owner=owner,
            status="completed",
            usage=exec_usage,
            last_event_offset=offset_counter,
            commit_effect=_commit_plan_result,
            committed_operation={
                "operation_type": "message_committed",
                "phase": "message_committed",
                "safety": "side_effect_committed",
                "payload": {"message_id": message_id},
            },
        )
        if completed:
            await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
        else:
            # 用户取消时 cancel_run 已先写终态、清租约，上面的 CAS 注定输掉。
            # 协作式取消让生成器正常收尾，本轮已完成的步骤和正文就在手里 ——
            # 不落库的话，用户回到会话只剩自己那句话。
            if _cancelled_by_user(run_id):
                _persist_plan_partial()
            else:
                logger.warning("plan_run_terminal_cas_lost", run_id=run_id, owner=owner)

    except asyncio.CancelledError:
        if lease_lost.is_set():
            logger.warning("plan_run_worker_fenced", run_id=run_id, owner=owner)
            if _cancelled_by_user(run_id):
                _persist_plan_partial()
        elif journal.complete(
            run_id,
            owner=owner,
            status="cancelled",
            failure_reason="plan worker cancelled",
            last_event_offset=offset_counter,
        ):
            logger.info("plan_run_cancelled", run_id=run_id)
            await _emit(
                {
                    "type": "plan_error",
                    "plan_id": plan_id,
                    "error": "任务已被用户取消",
                    "_cancelled": True,
                },
                terminal=True,
            )
            await _emit(
                {"type": _TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True},
                terminal=True,
            )

    except Exception as exc:
        nested_unknown = find_tool_outcome_unknown(exc)
        if nested_unknown is not None:
            await _pause_plan_tool_outcome(nested_unknown)
            return
        logger.error("plan_run_failed", run_id=run_id, error=str(exc), exc_info=True)
        failed = journal.complete(
            run_id,
            owner=owner,
            status="failed",
            failure_reason=str(exc)[:1000],
            last_event_offset=offset_counter,
        )
        if failed:
            await _emit(
                {"type": "plan_error", "plan_id": plan_id, "error": str(exc)[:200]},
                terminal=True,
            )
            await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)

    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        await _expire_stream(run_id)


def get_run_by_plan_id(plan_id: str) -> Optional[ChatRun]:
    """Reverse-lookup live run by plan_id (used by plan cancel endpoint)."""
    with SessionLocal() as db:
        return (
            db.query(ChatRun)
            .filter(
                ChatRun.status.in_(_LIVE_STATUSES),
                ChatRun.request_payload["plan_id"].astext == plan_id,
            )
            .order_by(ChatRun.created_at.desc())
            .first()
        )


# ─── Autonomous Loop (long-running autonomous execution, built on the same ChatRun framework) ───
async def start_autonomous_loop_run(
    *,
    loop_id: str,
    chat_id: str,
    user_id: str,
    goal_spec: Dict[str, Any],
    budget: Dict[str, Any],
    model_name: Optional[str] = None,
    model_provider_id: Optional[str] = None,
    evaluator_model: Optional[str] = None,
    worker_max_iters: int = 15,
    hitl_enabled: bool = False,
    enable_thinking: bool = False,
    chat_mode: Optional[str] = None,
    is_resume: bool = False,
    project_id: Optional[str] = None,
    automation_run: bool = False,
) -> ChatRun:
    """Start an autonomous-loop run (background task + Redis Stream, mirroring start_plan_execute_run)."""
    if not chat_id:
        raise ValueError("chat_id is required to start an autonomous loop run")
    request_payload = {
        "kind": "autonomous_loop",
        "loop_id": loop_id,
        "chat_id": chat_id,
        "goal_spec": goal_spec,
        "budget": budget,
        "model_name": model_name,
        "model_provider_id": model_provider_id,
        "evaluator_model": evaluator_model,
        "worker_max_iters": worker_max_iters,
        "hitl_enabled": hitl_enabled,
        "enable_thinking": enable_thinking,
        # Thinking level (fast/medium/high/max): the active-run probe endpoint
        # uses this to restore the frontend's resume phase; the worker uses it
        # to set reasoning_effort.
        "chat_mode": chat_mode,
        "is_resume": is_resume,
        "project_id": project_id,
        "automation_run": automation_run,
    }
    # 启动参数跟 loop 持久化（而非只活在本次请求里）：崩溃/重启后的续跑——无论 API
    # resume 还是启动自动续跑——都能还原同一套模型/评审模型/轮数/思考档位，不再悄悄
    # 降级到默认值。
    try:
        from core.services.loop_service import LoopService as _LoopSvc

        with SessionLocal() as db:
            _LoopSvc(db).save_start_params(
                loop_id,
                {
                    "model_name": model_name,
                    "model_provider_id": model_provider_id,
                    "evaluator_model": evaluator_model,
                    "worker_max_iters": worker_max_iters,
                    "hitl_enabled": hitl_enabled,
                    "enable_thinking": enable_thinking,
                    "chat_mode": chat_mode,
                    "automation_run": automation_run,
                },
            )
    except Exception:  # noqa: BLE001 - 参数存档失败不阻塞启动
        logger.warning("loop start_params persist failed", exc_info=True)
    run = _create_run_record(
        chat_id=chat_id,
        user_id=user_id,
        request_payload=request_payload,
        recovery_snapshot={
            "kind": "autonomous_loop",
            "worker_args": {
                "context": {
                    "model_name": model_name,
                    "model_provider_id": model_provider_id,
                    "chat_mode": chat_mode,
                    "project_ctx": {"project_id": project_id} if project_id else None,
                    "automation_run": automation_run,
                }
            },
        },
    )
    journal_owner = _new_worker_owner(run.run_id)
    _register_run_task(
        run.run_id,
        _run_autonomous_loop_workflow(
            run_id=run.run_id,
            loop_id=loop_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=run.message_id,
            goal_spec=goal_spec,
            budget=budget,
            model_name=model_name,
            model_provider_id=model_provider_id,
            evaluator_model=evaluator_model,
            worker_max_iters=worker_max_iters,
            hitl_enabled=hitl_enabled,
            enable_thinking=enable_thinking,
            chat_mode=chat_mode,
            is_resume=is_resume,
            project_id=project_id,
            automation_run=automation_run,
            journal_owner=journal_owner,
        ),
        name=f"autoloop_run:{run.run_id}",
    )
    logger.info(
        "autonomous_loop_run_started",
        run_id=run.run_id,
        loop_id=loop_id,
        user_id=user_id,
    )
    return run


_LOOP_STATUS_ZH = {
    "completed": "✅ 已达成",
    "budget_exhausted": "⏳ 预算耗尽",
    "cancelled": "⛔ 已取消",
    "awaiting_human": "🙋 等待人工",
    "failed": "❌ 失败",
}


def _loop_transcript_md(objective: str, result) -> str:
    """Render one autonomous loop's iteration trace into an assistant message for the chat (markdown)."""
    lines = [
        "**🔁 自主循环**",
        "",
        f"**目标**：{objective}",
        "",
        "| 轮次 | 需求 | 评审判定 | 工具 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in result.history:
        reason = str(r.get("reason") or "").replace("\n", " ").replace("|", "／")
        if len(reason) > 60:
            reason = reason[:60] + "…"
        lines.append(
            f"| {r.get('seq')} | {r.get('requirement_id', '')} | {r.get('verdict')} | "
            f"{r.get('tool_calls', 0)} | {reason} |"
        )
    status_zh = _LOOP_STATUS_ZH.get(result.status, result.status)
    # final_score is now the requirement pass ratio (0~1) — no script-based numeric score.
    final = "—" if result.final_score is None else f"{result.final_score:.0%}"
    lines += [
        "",
        f"**结果**：{status_zh} · 共 {result.iterations} 轮 · 完成度 {final}"
        f" · 用时 {result.wall_clock_s:.0f}s",
    ]
    if result.reason:
        lines.append(f"\n> {result.reason}")
    return "\n".join(lines)


async def _run_autonomous_loop_workflow(
    *,
    run_id: str,
    loop_id: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    goal_spec: Dict[str, Any],
    budget: Dict[str, Any],
    model_name: Optional[str],
    model_provider_id: Optional[str] = None,
    evaluator_model: Optional[str] = None,
    worker_max_iters: int = 15,
    hitl_enabled: bool = False,
    enable_thinking: bool = False,
    chat_mode: Optional[str] = None,
    is_resume: bool = False,
    project_id: Optional[str] = None,
    automation_run: bool = False,
    journal_owner: Optional[str] = None,
) -> None:
    from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop
    from orchestration.loop_evaluator import GoalSpec

    owner = journal_owner or _new_worker_owner(run_id)
    journal = _journal()
    if not journal.claim(run_id, owner=owner, lease_seconds=_RUN_LEASE_SECONDS):
        logger.info("autonomous_loop_claim_skipped", run_id=run_id, owner=owner)
        return
    current = asyncio.current_task()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _lease_heartbeat(run_id, owner, current, lease_lost),
        name=f"autonomous_loop_lease:{run_id}",
    )
    offset_counter = 0
    # Accumulate the worker's streamed body text + tool cards, and persist them
    # as an assistant message when the stream ends — exactly the same as a
    # normal conversation: the body only accumulates content deltas (thinking
    # goes through separate thinking events, is not stuffed into the body, and
    # no fake </think> is fabricated), and tools are persisted as tool_calls.
    # After a refresh, buildHistorySegments reconstructs identically to a normal
    # conversation.
    content_buf: List[str] = []
    tool_log: List[Dict[str, Any]] = []
    tool_idx: Dict[str, int] = {}

    async def _emit(event: Dict[str, Any], *, terminal: bool = False) -> None:
        nonlocal offset_counter
        offset_counter = journal.allocate_event_offset(
            run_id,
            owner=None if terminal else owner,
            terminal=terminal,
        )
        et = event.get("type")
        if et == "content":
            _delta = event.get("delta")
            if _delta:
                content_buf.append(str(_delta))
        elif et == "tool_call":
            _tid = str(event.get("tool_id") or f"t{len(tool_log)}")
            tool_idx[_tid] = len(tool_log)
            tool_log.append(
                {
                    "id": _tid,
                    "name": event.get("tool_name") or "tool",
                    "input": event.get("tool_args"),
                    "status": "running",
                }
            )
        elif et == "tool_result":
            _i = tool_idx.get(str(event.get("tool_id") or ""))
            if _i is not None:
                tool_log[_i]["output"] = event.get("result")
                tool_log[_i]["status"] = "error" if event.get("error") else "success"
        await _xadd_event(run_id, offset_counter, event)
        # At structural checkpoints (requirement flipped / per-iteration
        # evaluation done) persist progress incrementally — so after a mid-run
        # crash/restart/refresh the already-produced body + tool cards are still
        # visible from the DB, rather than waiting for a single write at the
        # terminal state (see the fix for symptoms 2/3).
        if et in ("requirement_passed", "iteration_evaluated"):
            _flush_loop_message()

    def _loop_message_effect(status: str):
        if not conversational:
            return None
        body = "".join(content_buf).strip()
        captured_tools = [dict(item) for item in tool_log]
        if not body and not captured_tools:
            return None

        def _commit(db) -> None:  # noqa: ANN001
            ChatService(db).upsert_message(
                chat_id=chat_id,
                role="assistant",
                content=body,
                message_id=message_id,
                tool_calls=captured_tools or None,
                extra_data={
                    "autonomous_loop": True,
                    "loop_id": loop_id,
                    "loop_status": status,
                },
                commit=False,
            )

        return _commit

    def _persist_loop_partial() -> None:
        effect = _loop_message_effect("cancelled")
        if effect is None:
            return
        try:
            with SessionLocal() as _db:
                effect(_db)
                _db.commit()
        except Exception:  # noqa: BLE001 - 落库失败不能吞掉取消收尾
            logger.warning(
                "autonomous_loop_cancelled_partial_persist_failed",
                run_id=run_id,
                exc_info=True,
            )

    def _flush_loop_message(status: str = "running") -> None:
        """Upsert the currently accumulated worker body + tool cards into the assistant message (same message_id).

        Conversational mode (self_verify) only. Skipped when output is empty
        (no body and no tools yet) to avoid writing an empty bubble.
        """
        commit_effect = _loop_message_effect(status)
        if commit_effect is None:
            return
        try:
            journal.append_operation(
                run_id,
                owner=owner,
                operation_type="loop_checkpoint_committed",
                phase="loop_checkpoint",
                safety="side_effect_committed",
                payload={"loop_id": loop_id, "message_id": message_id},
                commit_effect=commit_effect,
            )
        except RunLeaseLost:
            raise
        except Exception:  # noqa: BLE001 - incremental persist failure must not take down the run
            logger.warning("loop incremental persist failed", exc_info=True)

    gs = GoalSpec(
        objective=goal_spec.get("objective", ""),
        acceptance_criteria=goal_spec.get("acceptance_criteria", []) or [],
    )
    # 预算去一等公民化：缺省一律 0（不限）。「能完成任务」优先——停止条件回归
    # 账本全通过/停滞无解/用户取消；防失控由 LOOP_HARD_MAX_ITERS 硬后备兜底。
    # 显式传正数的旧 loop 行（历史数据）仍按其预算执行。
    bud = LoopBudget(
        max_iters=int(budget.get("max_iters", 0) or 0),
        max_wall_clock_s=float(budget.get("max_wall_clock_s", 0) or 0),
        max_tokens=int(budget.get("max_tokens", 0) or 0),
    )
    # Project binding: resolve project_ctx and bind the loop's chat session to
    # that project — this scopes the worker's/reviewer's file tools to the
    # project folder (where the site source lives), and lets publish_site
    # reverse-look-up the project from the session and publish to the same site.
    # When bound to a project, the sandbox session uses chat_id (same as a
    # normal project conversation → project file materialization and publish
    # packaging share one session); without a project it degrades to the
    # isolated loop-{loop_id} (pure task-style loop).
    project_ctx: Optional[Dict[str, Any]] = None
    session_id = f"loop-{loop_id}"
    # Autonomous loops always live on in the chat history as a "normal
    # conversation" (the objective is persisted as a user message and the result
    # as an assistant message, surviving refreshes). Script-verification/form
    # modes have been removed; there is no non-conversational branch anymore.
    conversational = True

    # Requirement-ledger DB mirror callbacks: every time the driver writes the
    # ledger it is also synced into agent_loops.metadata; resume reads the DB
    # first (reliable across rebuilds/restarts/machine changes) and no longer
    # depends on whether the sandbox /workspace still exists (see the fix for
    # symptom 1).
    from core.services.loop_service import LoopService as _LoopSvc

    def _load_ledger() -> Optional[Dict[str, Any]]:
        try:
            with SessionLocal() as db:
                return _LoopSvc(db).load_ledger(loop_id)
        except Exception:  # noqa: BLE001
            logger.warning("loop load_ledger failed", exc_info=True)
            return None

    def _save_ledger(led: Dict[str, Any]) -> None:
        def _commit(db) -> None:  # noqa: ANN001
            _LoopSvc(db).save_ledger(loop_id, led, commit=False)

        journal.append_operation(
            run_id,
            owner=owner,
            operation_type="loop_ledger_committed",
            phase="loop_checkpoint",
            safety="side_effect_committed",
            payload={"loop_id": loop_id},
            commit_effect=_commit,
        )

    def _poll_steering() -> List[str]:
        """每轮开工前取走用户运行中追加的指令（POST /v1/loops/{id}/steer）。"""
        try:
            found: List[str] = []

            def _commit(db) -> None:  # noqa: ANN001
                found.extend(_LoopSvc(db).consume_steering(loop_id, commit=False))

            journal.append_operation(
                run_id,
                owner=owner,
                operation_type="loop_steering_polled",
                phase="loop_checkpoint",
                safety="side_effect_committed",
                payload={"loop_id": loop_id},
                commit_effect=_commit,
            )
            return found
        except RunLeaseLost:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("loop consume_steering failed", exc_info=True)
            return []

    try:
        if project_id:
            try:
                from core.db.models import ChatSession
                from core.services.project_scope import build_project_ctx

                needs_binding = False
                with SessionLocal() as db:
                    project_ctx = build_project_ctx(db, project_id)
                    if project_ctx:
                        sess = db.get(ChatSession, chat_id)
                        needs_binding = sess is not None and sess.project_id != project_id

                if project_ctx and needs_binding:

                    def _commit_project_binding(db) -> None:  # noqa: ANN001
                        sess = db.get(ChatSession, chat_id)
                        if sess is not None:
                            sess.project_id = project_id

                    journal.append_operation(
                        run_id,
                        owner=owner,
                        operation_type="loop_project_bound",
                        phase="pre_model",
                        safety="side_effect_committed",
                        payload={"loop_id": loop_id, "project_id": project_id},
                        commit_effect=_commit_project_binding,
                    )
            except RunLeaseLost:
                raise
            except Exception:  # noqa: BLE001 - project lookup may safely degrade
                logger.warning("loop project_ctx resolve failed", exc_info=True)
                project_ctx = None
            session_id = chat_id if project_ctx else f"loop-{loop_id}"

        await _emit(
            {
                "type": "run_started",
                "run_id": run_id,
                "message_id": message_id,
                "chat_id": chat_id,
                "kind": "autonomous_loop",
                "loop_id": loop_id,
            }
        )

        def _commit_loop_start(db) -> None:  # noqa: ANN001
            from core.services.loop_service import LoopService

            if conversational and gs.objective and not is_resume:
                # Only the first start persists the objective; the deterministic
                # id also makes a transaction retry idempotent.
                ChatService(db).add_message(
                    chat_id=chat_id,
                    role="user",
                    content=gs.objective,
                    message_id=f"{message_id}_objective",
                    extra_data={"autonomous_loop": True, "loop_id": loop_id},
                    commit=False,
                )
            LoopService(db).mark_running(
                loop_id,
                workspace_session=session_id,
                commit=False,
            )

        journal.append_operation(
            run_id,
            owner=owner,
            operation_type="loop_started_committed",
            phase="pre_model",
            safety="side_effect_committed",
            payload={"loop_id": loop_id},
            commit_effect=_commit_loop_start,
        )

        from core.auth.tenancy import tenant_of
        from core.llm.middlewares import CURRENT_RUN_BINDING

        binding_token = CURRENT_RUN_BINDING.set((run_id, owner))
        try:
            result = await run_autonomous_loop(
                loop_id=loop_id,
                user_id=user_id,
                goal_spec=gs,
                budget=bud,
                model_name=model_name,
                model_provider_id=model_provider_id,
                # 评审/规划模型：显式指定 > 「模型管理 → 角色分配 → loop_reviewer」>
                # main_agent。不再硬编码 "fast"——评审质量值得一个后台可配的位置。
                evaluator_model=evaluator_model,
                worker_max_iters=worker_max_iters,
                session_id=session_id,
                hitl_enabled=hitl_enabled,
                enable_thinking=enable_thinking,
                chat_mode=chat_mode,
                emit=_emit,
                is_cancelled=lambda: is_run_cancelled(run_id),
                load_ledger=_load_ledger,
                save_ledger=_save_ledger,
                poll_steering=_poll_steering,
                project_ctx=project_ctx,
                chat_id=chat_id,
                automation_run=automation_run,
                # Carried explicitly so the loop resolves *this* tenant's
                # orchestration profile. Omitting it is how one tenant's published
                # retry counts and budget multiplier became everyone's.
                tenant_id=tenant_of(user_id),
            )
        finally:
            CURRENT_RUN_BINDING.reset(binding_token)

        _asst_content: Optional[str] = None
        if conversational:
            # Prefer storing the streamed body (matches what the frontend saw); fall back to the trace table when the worker produced no body at all.
            _body = "".join(content_buf).strip()
            _status_zh = _LOOP_STATUS_ZH.get(result.status, result.status)
            if _body:
                _asst_content = _body + (
                    f"\n\n---\n**{_status_zh}** · 共 {result.iterations} 轮"
                    + (f" · 最终分 {result.final_score}" if result.final_score is not None else "")
                )
            else:
                _asst_content = _loop_transcript_md(gs.objective, result)

        def _commit_loop_result(db) -> None:  # noqa: ANN001
            from core.services.loop_service import LoopService

            LoopService(db).persist_result(loop_id, result, commit=False)
            if _asst_content is not None:
                # Upsert and the ChatRun terminal transition share one owner-
                # fenced transaction, so a superseded worker cannot publish.
                ChatService(db).upsert_message(
                    chat_id=chat_id,
                    role="assistant",
                    content=_asst_content,
                    usage={"total_tokens": result.tokens_spent},
                    tool_calls=tool_log if tool_log else None,
                    extra_data={
                        "autonomous_loop": True,
                        "loop_id": loop_id,
                        "loop_status": result.status,
                    },
                    message_id=message_id,
                    commit=False,
                )

        run_status = (
            "completed"
            if result.status in ("completed", "budget_exhausted", "awaiting_human")
            else ("cancelled" if result.status == "cancelled" else "failed")
        )
        completed = journal.complete(
            run_id,
            owner=owner,
            status=run_status,
            usage={"total_tokens": result.tokens_spent},
            last_event_offset=offset_counter,
            commit_effect=_commit_loop_result,
            committed_operation={
                "operation_type": "loop_result_committed",
                "phase": "loop_result_committed",
                "safety": "side_effect_committed",
                "payload": {"loop_id": loop_id, "message_id": message_id},
            },
        )
        if completed:
            await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
        else:
            logger.warning("autonomous_loop_terminal_cas_lost", run_id=run_id, owner=owner)
    except asyncio.CancelledError:
        # Cancelled: run_autonomous_loop was interrupted and no result is
        # available, so the terminal-state persistence block is skipped — here
        # we persist the progress produced so far (symptoms 2/3); otherwise a
        # reopened chat would only contain the user's objective.
        if lease_lost.is_set():
            logger.warning("autonomous_loop_worker_fenced", run_id=run_id, owner=owner)
            if _cancelled_by_user(run_id):
                _persist_loop_partial()
        else:
            partial_effect = _loop_message_effect("cancelled")
            cancelled = journal.complete(
                run_id,
                owner=owner,
                status="cancelled",
                failure_reason="autonomous loop worker cancelled",
                last_event_offset=offset_counter,
                commit_effect=partial_effect,
                committed_operation=(
                    {
                        "operation_type": "loop_partial_message_committed",
                        "phase": "loop_partial_message_committed",
                        "safety": "side_effect_committed",
                        "payload": {"loop_id": loop_id, "message_id": message_id},
                    }
                    if partial_effect is not None
                    else None
                ),
            )
            if not cancelled:
                return
            await _emit(
                {"type": "loop_error", "error": "任务已被用户取消", "_cancelled": True},
                terminal=True,
            )
            await _emit(
                {"type": _TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True},
                terminal=True,
            )
    except Exception as exc:  # noqa: BLE001
        nested_unknown = find_tool_outcome_unknown(exc)
        if nested_unknown is not None:
            if journal.needs_attention(
                run_id,
                owner=owner,
                reason=f"tool outcome pending policy recovery: {nested_unknown}",
            ):
                await _emit(
                    {
                        "type": "loop_error",
                        "error": "工具调用结果无法安全确定，任务已暂停等待处理",
                    },
                    terminal=True,
                )
                await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
            return
        logger.exception("autonomous_loop_run_failed", run_id=run_id)
        partial_effect = _loop_message_effect("failed")
        failed = journal.complete(
            run_id,
            owner=owner,
            status="failed",
            failure_reason=str(exc)[:1000],
            last_event_offset=offset_counter,
            commit_effect=partial_effect,
            committed_operation=(
                {
                    "operation_type": "loop_partial_message_committed",
                    "phase": "loop_partial_message_committed",
                    "safety": "side_effect_committed",
                    "payload": {"loop_id": loop_id, "message_id": message_id},
                }
                if partial_effect is not None
                else None
            ),
        )
        if failed:
            await _emit(
                {"type": "loop_error", "error": str(exc)[:500]},
                terminal=True,
            )
            await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        await _expire_stream(run_id)


# ─── Plan Generate (Phase 1: decoupled from HTTP) ─────────────────────


async def start_plan_generate_run(
    *,
    chat_id: str,
    user_id: str,
    task_description: str,
    model_name: str = DEFAULT_CHAT_MODEL_ALIAS,
    model_provider_id: Optional[str] = None,
    enabled_mcp_ids: Optional[List[str]] = None,
    enabled_skill_ids: Optional[List[str]] = None,
    enabled_kb_ids: Optional[List[str]] = None,
    enabled_agent_ids: Optional[List[str]] = None,
    session_messages: List[Dict[str, Any]],
    uploaded_files: Optional[List[Dict[str, Any]]] = None,
) -> ChatRun:
    """Plan Phase-1 (generate): background task on the same ChatRun infrastructure."""
    if not chat_id:
        raise ValueError("chat_id is required to start a plan generate run")

    request_payload = {
        "kind": "plan_generate",
        "chat_id": chat_id,
        "task_description": task_description[:500],
        "model_name": model_name,
        **({"model_provider_id": model_provider_id} if model_provider_id else {}),
    }
    recovery_snapshot = {
        "kind": "plan_generate",
        "worker_args": {
            "task_description": task_description,
            "model_name": model_name,
            "model_provider_id": model_provider_id,
            "enabled_mcp_ids": list(enabled_mcp_ids or []),
            "enabled_skill_ids": list(enabled_skill_ids or []),
            "enabled_kb_ids": list(enabled_kb_ids or []),
            "enabled_agent_ids": list(enabled_agent_ids or []),
            "session_messages": list(session_messages or []),
            "uploaded_files": list(uploaded_files or []),
        },
    }
    run = _create_run_record(
        chat_id=chat_id,
        user_id=user_id,
        request_payload=request_payload,
        recovery_snapshot=recovery_snapshot,
    )
    journal_owner = _new_worker_owner(run.run_id)
    _register_run_task(
        run.run_id,
        _run_plan_generate_workflow(
            run_id=run.run_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=run.message_id,
            task_description=task_description,
            model_name=model_name,
            model_provider_id=model_provider_id,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_skill_ids=enabled_skill_ids,
            enabled_kb_ids=enabled_kb_ids,
            enabled_agent_ids=enabled_agent_ids,
            session_messages=session_messages,
            uploaded_files=uploaded_files,
            journal_owner=journal_owner,
        ),
        name=f"plan_gen_run:{run.run_id}",
    )
    logger.info("plan_generate_run_started", run_id=run.run_id, chat_id=chat_id)
    return run


async def _run_plan_generate_workflow(
    *,
    run_id: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    task_description: str,
    model_name: str,
    model_provider_id: Optional[str],
    enabled_mcp_ids: Optional[List[str]],
    enabled_skill_ids: Optional[List[str]],
    enabled_kb_ids: Optional[List[str]],
    enabled_agent_ids: Optional[List[str]],
    session_messages: List[Dict[str, Any]],
    uploaded_files: Optional[List[Dict[str, Any]]],
    journal_owner: Optional[str] = None,
) -> None:
    from orchestration.subagents.plan_mode import astream_generate_plan

    owner = journal_owner or _new_worker_owner(run_id)
    journal = _journal()
    if not journal.claim(run_id, owner=owner, lease_seconds=_RUN_LEASE_SECONDS):
        logger.info("plan_generate_claim_skipped", run_id=run_id, owner=owner)
        return
    try:
        journal.append_operation(
            run_id,
            owner=owner,
            operation_type="worker_started",
            phase="pre_model",
            safety="replayable",
        )
    except RunLeaseLost:
        logger.warning("plan_generate_fenced_before_start", run_id=run_id, owner=owner)
        return
    worker_task = asyncio.current_task()
    if worker_task is None:  # pragma: no cover - asyncio always owns this coroutine
        raise RuntimeError("plan generate worker has no asyncio task")
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _lease_heartbeat(run_id, owner, worker_task, lease_lost),
        name=f"plan_generate_lease:{run_id}",
    )
    offset_counter = 0

    async def _emit(event: Dict[str, Any], *, terminal: bool = False) -> None:
        nonlocal offset_counter
        offset_counter = journal.allocate_event_offset(
            run_id,
            owner=None if terminal else owner,
            terminal=terminal,
        )
        await _xadd_event(run_id, offset_counter, event)

    plan_id_out: Optional[str] = None
    plan_title = ""
    plan_desc = ""
    plan_snapshot: Optional[Dict[str, Any]] = None
    plan_record: Optional[Dict[str, Any]] = None
    assistant_content = ""
    gen_usage: Optional[Dict[str, Any]] = None

    try:
        await _emit(
            {
                "type": "run_started",
                "run_id": run_id,
                "message_id": message_id,
                "chat_id": chat_id,
                "kind": "plan_generate",
            }
        )

        from core.llm.middlewares import CURRENT_RUN_BINDING

        binding_token = CURRENT_RUN_BINDING.set((run_id, owner))
        try:
            with SessionLocal() as stream_db:
                async for event in astream_generate_plan(
                    task_description=task_description,
                    user_id=user_id,
                    db=stream_db,
                    model_name=model_name,
                    model_provider_id=model_provider_id,
                    enabled_mcp_ids=enabled_mcp_ids,
                    enabled_skill_ids=enabled_skill_ids,
                    enabled_kb_ids=enabled_kb_ids,
                    enabled_agent_ids=enabled_agent_ids,
                    session_messages=session_messages,
                    uploaded_files=uploaded_files,
                    chat_id=chat_id,
                ):
                    if event.get("type") == "plan_generated":
                        plan_record = dict(event)
                        plan_id_out = event.get("plan_id")
                        plan_title = event.get("title", "")
                        plan_desc = event.get("description", "")
                        steps = event.get("steps", [])
                        step_summary = "\n".join(
                            f"{i + 1}. {s.get('title', '')}" for i, s in enumerate(steps)
                        )
                        assistant_content = (
                            f"已生成执行计划：**{plan_title}**\n\n"
                            f"{plan_desc}\n\n"
                            f"**执行步骤：**\n{step_summary}"
                        )
                        plan_snapshot = {
                            "mode": "preview",
                            "title": plan_title,
                            "description": plan_desc,
                            "steps": [
                                {
                                    "step_order": s.get("step_order", i + 1),
                                    "title": s.get("title", ""),
                                    "description": s.get("description"),
                                    "expected_tools": s.get("expected_tools", []),
                                    "expected_skills": s.get("expected_skills", []),
                                }
                                for i, s in enumerate(steps)
                            ],
                            "total_steps": len(steps),
                            "completed_steps": 0,
                        }
                        gen_usage = event.get("usage") or None
                    await _emit(event)
        finally:
            CURRENT_RUN_BINDING.reset(binding_token)

        def _commit_plan_preview(persist_db) -> None:  # noqa: ANN001
            if not assistant_content or not plan_id_out or plan_record is None:
                return
            from core.services.plan_service import PlanService

            extra_data: Dict[str, Any] = {}
            if uploaded_files:
                extra_data["uploaded_files"] = list(uploaded_files)
            agent_name_map = plan_record.get("agent_name_map")
            if isinstance(agent_name_map, dict) and agent_name_map:
                extra_data["agent_name_map"] = dict(agent_name_map)
            PlanService(persist_db).create_plan(
                plan_id=str(plan_id_out),
                user_id=user_id,
                title=str(plan_record.get("title") or "未命名计划"),
                description=str(plan_record.get("description") or ""),
                task_input=task_description,
                steps=list(plan_record.get("steps") or []),
                extra_data=extra_data,
                commit=False,
            )
            ChatService(persist_db).add_message(
                chat_id=chat_id,
                role="assistant",
                content=assistant_content,
                model=model_name,
                message_id=message_id,
                extra_data={
                    "is_markdown": True,
                    "plan_id": plan_id_out,
                    "plan_snapshot": plan_snapshot,
                    "message_id": message_id,
                },
                usage=gen_usage,
                commit=False,
            )

        completed = journal.complete(
            run_id,
            owner=owner,
            status="completed",
            usage=gen_usage,
            last_event_offset=offset_counter,
            commit_effect=_commit_plan_preview,
            committed_operation={
                "operation_type": "message_committed",
                "phase": "message_committed",
                "safety": "side_effect_committed",
                "payload": {"message_id": message_id, "plan_id": plan_id_out},
            },
        )
        if completed:
            await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
        else:
            logger.warning("plan_generate_terminal_cas_lost", run_id=run_id, owner=owner)

    except asyncio.CancelledError:
        if lease_lost.is_set():
            logger.warning("plan_generate_worker_fenced", run_id=run_id, owner=owner)
        else:
            cancelled = journal.complete(
                run_id,
                owner=owner,
                status="cancelled",
                failure_reason="plan generation worker cancelled",
                last_event_offset=offset_counter,
            )
            current = get_run(run_id)
            if cancelled or (current is not None and current.status == "cancelled"):
                logger.info("plan_generate_run_cancelled", run_id=run_id)
            else:
                logger.warning(
                    "plan_generate_cancel_cas_lost",
                    run_id=run_id,
                    owner=owner,
                )
                return
            await _emit(
                {
                    "type": "plan_error",
                    "error": "任务已被用户取消",
                    "_cancelled": True,
                },
                terminal=True,
            )
            await _emit(
                {"type": _TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True},
                terminal=True,
            )

    except Exception as exc:
        nested_unknown = find_tool_outcome_unknown(exc)
        if nested_unknown is not None:
            if journal.needs_attention(
                run_id,
                owner=owner,
                reason=f"tool outcome pending policy recovery: {nested_unknown}",
            ):
                await _emit(
                    {
                        "type": "plan_error",
                        "error": "工具调用结果无法安全确定，任务已暂停等待处理",
                    },
                    terminal=True,
                )
                await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
            return
        logger.error("plan_generate_run_failed", run_id=run_id, error=str(exc), exc_info=True)
        failed = journal.complete(
            run_id,
            owner=owner,
            status="failed",
            failure_reason=str(exc)[:1000],
            last_event_offset=offset_counter,
        )
        if failed:
            await _emit(
                {"type": "plan_error", "error": str(exc)[:200]},
                terminal=True,
            )
            await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)

    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await _expire_stream(run_id)


# ─── Cancellation ──────────────────────────────────────────────────────


async def cancel_run(run_id: str, *, user_id: str) -> bool:
    """Mark status=cancelled. Idempotent on terminal runs.

    For chat-run kinds (regular chat / plan-generate) we forcefully cancel the
    underlying asyncio task — these don't hold long-lived per-step HTTP MCP
    clients so anyio cancel propagation is safe.

    For plan-execute kinds we DO NOT call task.cancel(). plan_mode polls
    is_run_cancelled() between LLM streaming heartbeats and self-cancels
    inside its own task. Cross-task task.cancel() on a plan-execute worker
    risks an anyio cancel-scope deadlock: the worker holds per-step
    streamable_http MCP clients whose internal SSE tasks may be blocked on
    socket recv at cancel time, causing anyio's _deliver_cancellation to
    self-reschedule every 10ms and starve the whole event loop.
    """
    run = get_run(run_id)
    if run is None:
        raise ChatRunNotFound(f"chat run {run_id} not found")
    if run.user_id != user_id:
        raise ChatRunPermissionDenied(f"run {run_id} owned by another user")
    if run.status not in _LIVE_STATUSES:
        return False

    if not _journal().cancel(run_id, reason="user cancelled"):
        # Race: the worker/reaper just beat us to writing the terminal state — treat as "no longer running".
        return False

    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    cooperative_only = payload.get("kind") in _COOPERATIVE_KINDS

    # Cooperative workers are fenced by the cancelled DB row and therefore
    # deliberately stay silent when their heartbeat cancels them. Project the
    # authoritative cancellation here so followers never wait on the old task.
    if cooperative_only:
        await _write_terminal_to_stream(
            run_id,
            chat_id=run.chat_id,
            error_text="任务已被用户取消",
            cancelled=True,
        )

    task = _active_runs.get(run_id)
    if task is not None and not task.done():
        if cooperative_only:
            target = asyncio.shield(task)
            timeout = 20.0
        else:
            task.cancel()
            target = task
            timeout = 2.0
        try:
            await asyncio.wait_for(target, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.warning("cancel_run_await_failed", run_id=run_id, error=str(exc))
        if task.done():
            _acknowledge_terminal_writer(run_id)
    elif not cooperative_only:
        # Cross-process / post-restart: worker not in this process. Inject the
        # terminal markers ourselves so any active follower exits cleanly.
        await _write_terminal_to_stream(
            run_id,
            chat_id=run.chat_id,
            error_text="任务已被用户取消",
            cancelled=True,
        )

    return True


async def recover_orphan_runs() -> int:
    """Recover claimable runs from their last committed database safe point."""
    from core.services.tool_effect_ledger import ToolEffectJournal, recover_incomplete_tool_effects

    effect_decisions = await recover_incomplete_tool_effects(
        journal=ToolEffectJournal(SessionLocal)
    )
    effect_attention_runs = {
        item.run_id for item in effect_decisions if item.action == "needs_attention"
    }
    for run_id in effect_attention_runs:
        run = get_run(run_id)
        if run is not None:
            await _write_terminal_to_stream(
                run_id,
                chat_id=run.chat_id or "",
                error_text="工具调用结果无法安全确定，任务已暂停等待处理",
            )
    decisions = _journal().recover()
    for decision in decisions:
        if decision.action == "resume":
            if not _register_recovered_chat(decision):
                reason = "run kind or recovery snapshot cannot be resumed automatically"
                if _journal().needs_attention(decision.run_id, reason=reason):
                    await _write_terminal_to_stream(
                        decision.run_id,
                        chat_id=decision.chat_id or "",
                        error_text="任务缺少可恢复快照，已暂停等待处理",
                    )
        elif decision.action == "resume_from_snapshot":
            snapshot_result = await _commit_recovered_chat_snapshot(decision)
            if snapshot_result is False:
                await _write_terminal_to_stream(
                    decision.run_id,
                    chat_id=decision.chat_id or "",
                    error_text="已保存的模型结果无法安全提交，任务已暂停等待处理",
                )
        elif decision.action == "needs_attention":
            if decision.phase == "model_inflight":
                recovery_error = "模型调用结果不确定，任务已暂停等待恢复决策"
            elif decision.phase == "legacy_unrecoverable":
                recovery_error = "旧版本任务缺少恢复快照，已暂停等待处理"
            else:
                recovery_error = "任务在工具结果不确定的安全边界暂停，等待恢复决策"
            await _write_terminal_to_stream(
                decision.run_id,
                chat_id=decision.chat_id or "",
                error_text=recovery_error,
            )
        elif decision.action == "completed":
            await _write_recovered_terminal_marker(
                decision.run_id,
                chat_id=decision.chat_id or "",
            )
        elif decision.action == "failed":
            await _write_terminal_to_stream(
                decision.run_id,
                chat_id=decision.chat_id or "",
                error_text="服务重启发生在任务准备完成前，请重新发起",
            )
    recovered_count = len({item.run_id for item in decisions} | effect_attention_runs)
    if recovered_count:
        logger.info("chat_run_orphan_recovered", count=recovered_count)
    return recovered_count


def _register_recovered_chat(decision: RecoveryDecision) -> bool:
    snapshot = dict(decision.snapshot or {})
    if snapshot.get("kind") != "chat":
        return False
    args = snapshot.get("worker_args")
    if not isinstance(args, dict):
        return False
    required = {
        "session_messages",
        "effective_user_message",
        "raw_user_message",
        "context",
    }
    if not required.issubset(args):
        return False
    owner = _new_worker_owner(decision.run_id)
    recovered_run = get_run(decision.run_id)
    _register_run_task(
        decision.run_id,
        _run_workflow(
            run_id=decision.run_id,
            chat_id=decision.chat_id,
            user_id=decision.user_id,
            message_id=decision.message_id,
            assistant_chat_seq=(
                int(recovered_run.assistant_chat_seq)
                if recovered_run is not None and recovered_run.assistant_chat_seq is not None
                else None
            ),
            session_messages=list(args.get("session_messages") or []),
            effective_user_message=str(args.get("effective_user_message") or ""),
            raw_user_message=str(args.get("raw_user_message") or ""),
            context=dict(args.get("context") or {}),
            model_name=(str(args["model_name"]) if args.get("model_name") else None),
            journal_owner=owner,
            recovering=True,
        ),
        name=f"chat_run_recovery:{decision.run_id}",
    )
    return True


async def _commit_recovered_chat_snapshot(decision: RecoveryDecision) -> Optional[bool]:
    snapshot = dict(decision.snapshot or {})
    content = snapshot.get("assistant_content")
    message_id = str(snapshot.get("message_id") or decision.message_id)
    if not isinstance(content, str) or not message_id:
        paused = _journal().needs_attention(
            decision.run_id,
            reason="model-completed snapshot is incomplete",
        )
        return False if paused else None
    owner = _new_worker_owner(decision.run_id)
    recovered_run = get_run(decision.run_id)
    journal = _journal()
    if not journal.claim(
        decision.run_id,
        owner=owner,
        lease_seconds=_RUN_LEASE_SECONDS,
    ):
        return None
    try:
        from core.services.artifact_service import persist_artifacts as _persist_artifacts
        from core.services.project_scope import project_scope_from_context

        queued_handoff: Dict[str, Any] = {}

        def _commit_recovered_output(db) -> None:
            ChatService(db).upsert_message(
                chat_id=decision.chat_id,
                role="assistant",
                content=content,
                message_id=message_id,
                chat_seq=(
                    int(recovered_run.assistant_chat_seq)
                    if recovered_run is not None and recovered_run.assistant_chat_seq is not None
                    else None
                ),
                model=(
                    str(snapshot["model_name"]) if snapshot.get("model_name") is not None else None
                ),
                thinking=(
                    list(snapshot["thinking"])
                    if isinstance(snapshot.get("thinking"), list)
                    else None
                ),
                tool_calls=(
                    list(snapshot["tool_calls"])
                    if isinstance(snapshot.get("tool_calls"), list)
                    else None
                ),
                usage=(
                    dict(snapshot["usage"]) if isinstance(snapshot.get("usage"), dict) else None
                ),
                extra_data=(
                    dict(snapshot["extra_data"])
                    if isinstance(snapshot.get("extra_data"), dict)
                    else {}
                ),
                commit=False,
            )
            artifacts = snapshot.get("artifacts")
            if isinstance(artifacts, list):
                context_snapshot = snapshot.get("context")
                _persist_artifacts(
                    db,
                    decision.user_id,
                    decision.chat_id,
                    artifacts,
                    scope=project_scope_from_context(
                        dict(context_snapshot) if isinstance(context_snapshot, dict) else {}
                    ),
                    commit=False,
                )
            worker_args = snapshot.get("worker_args")
            recovery_args = worker_args if isinstance(worker_args, dict) else {}
            context_snapshot = snapshot.get("context")
            prepared = _commit_queued_handoff_in_session(
                db,
                source_run_id=decision.run_id,
                chat_id=decision.chat_id,
                user_id=decision.user_id,
                session_messages=(
                    list(recovery_args["session_messages"])
                    if isinstance(recovery_args.get("session_messages"), list)
                    else []
                ),
                assistant_content=content,
                context=(
                    dict(context_snapshot)
                    if isinstance(context_snapshot, dict)
                    else dict(recovery_args.get("context") or {})
                ),
                model_name=(
                    str(snapshot["model_name"])
                    if snapshot.get("model_name") is not None
                    else (
                        str(recovery_args["model_name"])
                        if recovery_args.get("model_name") is not None
                        else None
                    )
                ),
            )
            if prepared is not None:
                queued_handoff.update(prepared)

        completed = journal.complete(
            decision.run_id,
            owner=owner,
            status="completed",
            usage=(dict(snapshot["usage"]) if isinstance(snapshot.get("usage"), dict) else None),
            commit_effect=_commit_recovered_output,
            committed_operation={
                "operation_type": "message_committed_from_snapshot",
                "phase": "message_committed",
                "safety": "side_effect_committed",
                "payload": {"message_id": message_id},
            },
        )
        if completed:
            if queued_handoff:
                _schedule_queued_handoff(
                    queued_handoff,
                    chat_id=decision.chat_id,
                    user_id=decision.user_id,
                    task_prefix="chat_run_handoff_recovery",
                )
                await _write_queued_run_started_projection(
                    decision.run_id,
                    chat_id=decision.chat_id,
                    handoff=queued_handoff,
                )
            await _write_recovered_terminal_marker(
                decision.run_id,
                chat_id=decision.chat_id,
            )
        return completed
    except Exception as exc:  # noqa: BLE001 - pause instead of replaying model output
        logger.error(
            "chat_run_snapshot_commit_failed",
            run_id=decision.run_id,
            error=str(exc),
            exc_info=True,
        )
        paused = journal.needs_attention(
            decision.run_id,
            reason=str(exc)[:1000],
            owner=owner,
        )
        return False if paused else None


async def _write_recovered_terminal_marker(run_id: str, *, chat_id: str) -> None:
    event = {"type": _TERMINAL_TYPE, "chat_id": chat_id, "_recovered": True}
    try:
        offset = _journal().allocate_event_offset(run_id, terminal=True)
        await _xadd_event(run_id, offset, event)
        await _expire_stream(run_id)
    except Exception as exc:  # Redis is only a projection
        logger.warning("chat_run_recovered_terminal_write_failed", run_id=run_id, error=str(exc))


async def resume_running_loops() -> int:
    """启动对账 + 按需自动续跑：进程重启后收养/归位孤儿自主循环。

    进程刚起来时不存在任何活跃 task，因此 status='running' 的 loop 全是孤儿。
    对每一个孤儿：

    - ``LOOP_AUTO_RESUME=true`` → 用**持久化的启动参数**（agent_loops.extra_data.
      start_params：模型/评审模型/轮数/思考档位）原样续跑——账本在 DB 有镜像、
      沙箱有 feature_list.json，driver 自动断点续跑，不再悄悄降级到默认模型。
    - 关闭（默认）→ 状态归位为 ``interrupted``（可续跑），不再留下永远 running 的
      僵尸行（历史坑：僵尸 running 行既误导列表页、又让 cancel 无处下手）。

    'awaiting_human'/终态一律不动。
    """
    from core.db.models import AgentLoop

    auto = os.getenv("LOOP_AUTO_RESUME", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    resumed = 0
    with SessionLocal() as db:
        loops = db.query(AgentLoop).filter(AgentLoop.status == "running").all()
        specs = [
            (
                x.loop_id,
                x.chat_id,
                x.user_id,
                dict(x.goal_spec or {}),
                dict(x.budget or {}),
                (x.extra_data or {}).get("project_id"),
                dict((x.extra_data or {}).get("start_params") or {}),
            )
            for x in loops
        ]

    if not auto:
        if specs:
            from core.services.loop_service import LoopService as _LoopSvc

            with SessionLocal() as db:
                for loop_id, *_rest in specs:
                    _LoopSvc(db).mark_interrupted(
                        loop_id, reason="服务重启导致运行中断，可点击「继续」从断点续跑"
                    )
            logger.info("[startup] orphan loops marked interrupted: %d", len(specs))
        return 0

    for loop_id, chat_id, user_id, goal_spec, budget, project_id, params in specs:
        if not chat_id:
            continue
        try:
            await start_autonomous_loop_run(
                loop_id=loop_id,
                chat_id=chat_id,
                user_id=user_id,
                goal_spec=goal_spec,
                budget=budget,
                model_name=params.get("model_name"),
                model_provider_id=params.get("model_provider_id"),
                evaluator_model=params.get("evaluator_model"),
                worker_max_iters=int(params.get("worker_max_iters") or 15),
                hitl_enabled=bool(params.get("hitl_enabled")),
                enable_thinking=bool(params.get("enable_thinking")),
                chat_mode=params.get("chat_mode"),
                project_id=project_id,
                automation_run=bool(params.get("automation_run")),
                is_resume=True,
            )
            resumed += 1
            logger.info("autonomous_loop_resumed", loop_id=loop_id)
        except Exception:  # noqa: BLE001
            logger.warning("autonomous_loop_resume_failed loop_id=%s", loop_id, exc_info=True)
    return resumed


async def _stream_last_write_ms(run_id: str) -> Optional[int]:
    """Write time of the last event on the Redis Stream (epoch ms).

    Stream entry ids are naturally ``<ms>-<seq>``, so we just take the
    millisecond segment of the last entry — no extra bookkeeping. Returns None
    when there is no stream / no events / the read fails (callers treat that as
    "no activity").
    """
    redis = get_redis()
    try:
        entries = await redis.xrevrange(_stream_key(run_id), max="+", min="-", count=1)
    except Exception as exc:
        logger.warning("chat_run_activity_check_failed", run_id=run_id, error=str(exc))
        return None
    if not entries:
        return None
    entry_id = entries[0][0]
    if isinstance(entry_id, bytes):
        entry_id = entry_id.decode()
    try:
        return int(str(entry_id).split("-", 1)[0])
    except ValueError:
        return None


def _chat_has_live_job(chat_id: Optional[str], now: datetime) -> bool:
    """这个会话是否挂着**还在推进**的批量作业（跨进程可见的活性证据）。

    判据是 ``jobs.updated_at`` 在静默窗口内前进过：作业每完成一次子调用都会写 usage，
    onupdate 随之推进 updated_at。只要作业还在动，这个 run 就不是僵尸——哪怕它的
    asyncio task 属于另一个进程（多 worker / 多副本），也哪怕主链路一个流事件都没有。

    作业本身有 max_seconds 墙钟预算与熔断，所以这条豁免不会让 run 永生。
    """
    if not chat_id:
        return False
    try:
        from core.db.models import Job

        cutoff = now - timedelta(seconds=_STALE_QUIET_SEC)
        with SessionLocal() as db:
            return (
                db.query(Job.job_id)
                .filter(
                    Job.chat_id == chat_id,
                    Job.status.in_(("pending", "running")),
                    Job.updated_at >= cutoff,
                )
                .first()
                is not None
            )
    except Exception:  # noqa: BLE001 —— 证据查不到就按原判据走，绝不因此放过真僵尸
        return False


async def reap_stale_runs() -> int:
    """Periodic safety net: fail 'running' runs that show no sign of life.

    The verdict is activity-aware, in two tiers (both tunable via env vars):

    - Reap only when **over-age + quiet**: lifetime exceeds
      ``CHAT_RUN_MAX_AGE_SEC`` AND the Redis Stream's last write is older than
      ``CHAT_RUN_STALE_QUIET_SEC``. Long tasks still steadily producing
      tool_call/content are no longer falsely killed (historical bug: purely
      age-based reaping choked even active runs mid-tool-call once the 30-minute
      mark hit).
    - **Absolute cap**: runs older than ``CHAT_RUN_HARD_MAX_AGE_SEC`` are reaped
      even when actively emitting, except while a registered bounded human
      interaction is pending (that interaction keeps its own full deadline).

    Reaping also cancels the in-process worker task, keeping the DB terminal
    state aligned with the actual task (historical bug: only flipping the DB
    without killing the task let the worker silently run another 1.5h and
    overwrite the terminal state back to completed). plan_execute is the
    exception — cross-task cancel risks the anyio cancel-scope deadlock (see the
    ``cancel_run`` docstring); it stops itself after ``is_run_cancelled``
    polling observes the terminal state.

    Complements periodic ``recover_orphan_runs`` and the per-run
    inactivity watchdog — also sweeps up historical zombie runs left by
    older code paths.
    """
    from sqlalchemy import func

    now = _utcnow()
    cutoff = now - timedelta(seconds=_STALE_RUN_MAX_AGE_SEC)
    hard_cutoff = now - timedelta(seconds=_HARD_MAX_AGE_SEC)
    with SessionLocal() as db:
        candidates = (
            db.query(
                ChatRun.run_id,
                ChatRun.chat_id,
                ChatRun.request_payload,
                ChatRun.lease_expires_at,
                func.coalesce(ChatRun.started_at, ChatRun.created_at).label("began_at"),
            )
            .filter(ChatRun.status == "running")
            .filter(func.coalesce(ChatRun.started_at, ChatRun.created_at) < cutoff)
            .all()
        )
    if not candidates:
        return 0

    now_ms = int(now.timestamp() * 1000)
    reaped = 0
    for row in candidates:
        rid, cid = row.run_id, row.chat_id
        lease_expires_at = row.lease_expires_at
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
        began_at = row.began_at
        if began_at is not None and began_at.tzinfo is None:  # SQLite stores naive UTC
            began_at = began_at.replace(tzinfo=timezone.utc)
        hard_expired = began_at is not None and began_at < hard_cutoff
        if not hard_expired and lease_expires_at is not None and lease_expires_at > now:
            logger.info(
                "chat_run_stale_skip_live_lease",
                run_id=rid,
                lease_expires_at=lease_expires_at.isoformat(),
            )
            continue
        payload = row.request_payload if isinstance(row.request_payload, dict) else {}

        # 自主循环豁免统一年龄硬顶：loop 的使命就是「跑到任务完成为止」（去预算化），
        # 且它有自己的停滞护栏/熔断/硬后备轮数。进程内 task 还活着的 loop 绝不按
        # 年龄杀（历史竞态：6h 硬顶 == loop 旧默认预算，跑满预算的健康 loop 在优雅
        # 收尾前被硬顶抢杀，报错还误导为「无响应」）。孤儿 loop（进程重启遗留）
        # 走下面的静默判据正常清理。
        if hard_expired and payload.get("kind") == "autonomous_loop":
            task = _active_runs.get(rid)
            if task is not None and not task.done():
                logger.info("chat_run_stale_skip_loop_inprocess", run_id=rid)
                continue
            hard_expired = False  # 孤儿 loop：不按年龄硬杀，交给静默判据

        # A legitimate user decision may begin near the run's absolute-age
        # ceiling. Its own bounded wait deadline is the authority; do not cut a
        # newly opened question/confirmation down to the remainder of the run
        # hard cap. Once the registry resolves or times out, this exemption
        # disappears automatically on the next sweep.
        if hard_expired and human_interaction.has_pending(cid):
            logger.info("chat_run_stale_skip_human_wait", run_id=rid, chat_id=cid)
            continue

        # 工作流模式的批量作业：run 卡在 run_job(wait=True) 里等作业，期间主链路一个
        # 流事件都不产生（逐项结果按设计不进对话），"流静默"判据必然误判。进程内 task
        # 豁免只在本进程有效——多 worker / 多副本部署里，另一个进程看不到这个 task，
        # 照样会把健康的长作业杀掉。所以这里用**跨进程可见的证据**：DB 里这个会话是否
        # 还有正在推进的作业（每次子调用都会 add_usage → jobs.updated_at 前进）。
        if _chat_has_live_job(cid, now):
            logger.info("chat_run_stale_skip_live_job", run_id=rid, chat_id=cid)
            continue

        if not hard_expired:
            # A run whose asyncio task is alive in THIS process is never a
            # zombie — the in-process inactivity watchdog owns hang detection
            # for it. The stream-quiet check below is only for orphans whose
            # worker vanished (process restart/crash). Without this guard, a
            # healthy long run streaming huge tool-call args (which map to no
            # stream events) past 30min would be reaped as "quiet".
            task = _active_runs.get(rid)
            if task is not None and not task.done():
                logger.info("chat_run_stale_skip_inprocess", run_id=rid)
                continue
            last_ms = await _stream_last_write_ms(rid)
            if last_ms is not None and (now_ms - last_ms) < _STALE_QUIET_SEC * 1000:
                # The stream is still producing: this is a long task, not a zombie — skip this round
                logger.info(
                    "chat_run_stale_skip_active",
                    run_id=rid,
                    quiet_sec=round((now_ms - last_ms) / 1000, 1),
                )
                continue
            reason = "run stalled: no stream activity (stale watchdog)"
        else:
            reason = "run exceeded hard max age (stale watchdog)"

        if not _request_run_stop(
            rid,
            status="failed",
            error_message=reason,
            require_expired_lease=not hard_expired,
        ):
            continue  # race: the worker just beat us to writing the terminal state

        await _write_terminal_to_stream(
            rid,
            chat_id=cid or "",
            error_text="任务长时间无响应，已被系统中止，请重新发起",
        )

        payload = row.request_payload if isinstance(row.request_payload, dict) else {}
        task = _active_runs.get(rid)
        if task is not None and not task.done() and payload.get("kind") not in _COOPERATIVE_KINDS:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:
                logger.warning("stale_reaper_await_failed", run_id=rid, error=str(exc))
            if task.done():
                _acknowledge_terminal_writer(rid)
        reaped += 1

    if reaped:
        logger.info("chat_run_stale_reaped", count=reaped)
    return reaped


async def run_stale_reaper_loop() -> None:
    """Recover expired leases frequently and run the slower stale watchdog."""
    loop = asyncio.get_running_loop()
    next_reap_at = loop.time() + _STALE_REAPER_INTERVAL_SEC
    while True:
        try:
            await asyncio.sleep(_RUN_RECOVERY_INTERVAL_SEC)
            await recover_orphan_runs()
            if loop.time() >= next_reap_at:
                await reap_stale_runs()
                from core.services.tool_effect_ledger import ToolEffectJournal

                pruned = await asyncio.to_thread(
                    ToolEffectJournal(SessionLocal).prune_settled
                )
                if pruned:
                    logger.info("tool_effect_ledger_pruned", count=pruned)
                next_reap_at = loop.time() + _STALE_REAPER_INTERVAL_SEC
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("chat_run_stale_reaper_iteration_failed", exc_info=True)


def get_active_run_for_chat(chat_id: str, user_id: str) -> Optional[ChatRun]:
    """Backing query for GET /v1/chats/{chat_id}/active-run."""
    with SessionLocal() as db:
        rows = (
            db.query(ChatRun)
            .filter(
                ChatRun.chat_id == chat_id,
                ChatRun.user_id == user_id,
                (ChatRun.status.in_(_LIVE_STATUSES) | (ChatRun.writer_slot.isnot(None))),
            )
            .order_by(ChatRun.created_at.desc())
            .all()
        )
        for row in rows:
            payload = row.request_payload if isinstance(row.request_payload, dict) else {}
            if payload.get("kind") not in {
                "automation_plan",
                "automation_prompt",
                "batch_item",
                "internal_job_agent",
                "legacy_chat_stream",
            }:
                return row
        return None


# ─── SSE wire wrapper (shared by the chats / plans routes) ─────────────


async def follow_run_as_sse(
    run_id: str,
    *,
    chat_id: str,
    from_offset: int = 0,
    error_event_factory: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> AsyncIterator[str]:
    """Wrap follow_run as SSE wire frames.

    ``error_event_factory(reason)`` lets callers customize the error event shape
    (chat protocol vs plan protocol). Defaults to a chat-style ``{type: error}``.
    """

    def _default_err(reason: str) -> Dict[str, Any]:
        return {"type": "error", "error": reason, "chat_id": chat_id}

    factory = error_event_factory or _default_err

    # Decouple follow_run from the yield cadence via an intermediate queue: it
    # lets wait_for write an SSE comment line to the wire when the stream has
    # been silent longer than _HEARTBEAT_INTERVAL_SEC, serving as nginx /
    # reverse-proxy keepalive. Cancelling queue.get via wait_for is safe
    # (asyncio.Queue handles the cancel race); the underlying follow_run
    # coroutine is never interrupted.
    queue: asyncio.Queue = asyncio.Queue()

    async def _pump() -> None:
        try:
            async for event in follow_run(run_id, from_offset=from_offset):
                await queue.put(("event", event))
        except ChatRunNotFound:
            await queue.put(("not_found", None))
        except Exception as exc:  # noqa: BLE001
            await queue.put(("error", exc))
        else:
            await queue.put(("end", None))

    pump_task = asyncio.create_task(_pump(), name=f"sse_pump:{run_id}")
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL_SEC)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue

            if kind == "event":
                clean = {k: v for k, v in payload.items() if not k.startswith("_")}
                yield f"data: {json.dumps(clean, ensure_ascii=False)}\n\n"
            elif kind == "end":
                break
            elif kind == "not_found":
                yield f"data: {json.dumps(factory('run not found'), ensure_ascii=False)}\n\n"
                break
            elif kind == "error":
                logger.warning("follow_run_as_sse_failed", run_id=run_id, error=str(payload))
                yield f"data: {json.dumps(factory('流式响应中断'), ensure_ascii=False)}\n\n"
                break
    finally:
        pump_task.cancel()
        with contextlib.suppress(BaseException):
            await pump_task

    yield "data: [DONE]\n\n"
