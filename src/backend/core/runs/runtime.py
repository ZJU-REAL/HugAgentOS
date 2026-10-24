"""Query and cancel durable runs and track their local asyncio workers."""

from __future__ import annotations

import asyncio
import datetime as _datetime
import typing as _typing
from typing import Any, Awaitable, Callable, List, Optional, Tuple

import core.db.engine as engine
import core.db.models as models
import core.db.reserved_chats as reserved_chats
import core.runs.clock as clock
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.settings as settings
import core.runs.tasks as tasks
from core.db.models import ChatRun
from core.db.threads import run_db
from core.infra.logging import get_logger

logger = get_logger(__name__)


class ChatRunNotFound(Exception):
    pass


class ChatRunPermissionDenied(Exception):
    pass


def register_run_task(run_id: str, coro: Awaitable[None], *, name: str) -> None:
    """Spawn the worker coroutine, register it in _active_runs, auto-cleanup on done."""
    active = tasks.process_state().active_runs
    if run_id in active and not active[run_id].done():
        coro.close()
        return
    task = asyncio.create_task(coro, name=name)
    tasks.process_state().active_runs[run_id] = task

    def finished(worker):
        if active.get(run_id) is worker:
            active.pop(run_id, None)
        if not worker.cancelled() and worker.exception() is not None:
            logger.error(
                "run_worker_unhandled_error",
                run_id=run_id,
                error=str(worker.exception()),
                exc_info=worker.exception(),
            )

    task.add_done_callback(finished)


async def wait_run(run_id: str) -> ChatRun:
    """Wait for a locally launched run and return its durable terminal row.

    Shielding prevents a disconnected non-stream HTTP client from cancelling
    the detached worker. Explicit ``cancel_run`` still targets the registered
    task and keeps the database writer fence until that task has stopped.
    """

    task = tasks.process_state().active_runs.get(run_id)
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
    run = await run_db(get_run, run_id)
    if run is None:
        raise ChatRunNotFound(f"chat run {run_id} not found")
    if run.status in settings.LIVE_STATUSES:
        raise RuntimeError(f"chat run {run_id} lost its local worker before completion")
    return run


def get_run(run_id: str) -> Optional[ChatRun]:
    """Read a single run (used by the route layer for authorization)."""
    with engine.SessionLocal() as db:
        return db.query(models.ChatRun).filter(models.ChatRun.run_id == run_id).first()


def is_run_cancelled(run_id: str) -> bool:
    """Polled by plan_mode every ~15s heartbeat; see ``cancel_run`` for why.

    Any terminal state stops the worker (not just ``cancelled``): a run reaped
    to ``failed`` by the stale reaper must also make the cooperative worker stop
    itself as soon as possible, or we get the split-brain of "DB says dead, task
    keeps running". The worker itself only writes the terminal state during
    wrap-up, so any terminal state seen while polling must come from an external
    verdict.
    """
    with engine.SessionLocal() as db:
        row = db.query(models.ChatRun.status).filter(models.ChatRun.run_id == run_id).first()
        return bool(row and row[0] in settings._TERMINAL_STATUSES)


def _claim_cancellation(
    run_id: str,
    *,
    user_id: str,
    stop_terminal_worker: bool,
    cancel_guard: Optional[Callable[[Any], bool]],
) -> Optional[ChatRun]:
    """Write the cancelled state; returns the run, or None when there is nothing to stop."""
    run = get_run(run_id)
    if run is None:
        raise ChatRunNotFound(f"chat run {run_id} not found")
    if run.user_id != user_id:
        raise ChatRunPermissionDenied(f"run {run_id} owned by another user")
    already_cancelled = stop_terminal_worker and run.status == "cancelled"
    if run.status not in settings.LIVE_STATUSES and not already_cancelled:
        return None

    if already_cancelled and cancel_guard is not None:
        # Even terminal-worker cleanup needs current caller ownership. Release
        # the short guard transaction before stream or worker operations.
        with engine.SessionLocal() as db:
            if not cancel_guard(db):
                return None

    if not already_cancelled and not ownership.journal().cancel(
        run_id,
        reason="user cancelled",
        **({"cancel_guard": cancel_guard} if cancel_guard is not None else {}),
    ):
        # Race: the worker/reaper just beat us to writing the terminal state — treat as "no longer running".
        return None
    return run


async def cancel_run(
    run_id: str,
    *,
    user_id: str,
    stop_terminal_worker: bool = False,
    cancel_guard: Optional[Callable[[Any], bool]] = None,
) -> bool:
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
    run = await run_db(
        _claim_cancellation,
        run_id,
        user_id=user_id,
        stop_terminal_worker=stop_terminal_worker,
        cancel_guard=cancel_guard,
    )
    if run is None:
        return False

    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    cooperative_only = payload.get("kind") in settings._COOPERATIVE_KINDS

    # Cooperative workers are fenced by the cancelled DB row and therefore
    # deliberately stay silent when their heartbeat cancels them. Project the
    # authoritative cancellation here so followers never wait on the old task.
    if cooperative_only:
        await writer._write_terminal_to_stream(
            run_id,
            chat_id=run.chat_id,
            error_text="任务已被用户取消",
            cancelled=True,
        )

    task = tasks.process_state().active_runs.get(run_id)
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
            await run_db(ownership._acknowledge_terminal_writer, run_id)
    elif not cooperative_only:
        # Cross-process / post-restart: worker not in this process. Inject the
        # terminal markers ourselves so any active follower exits cleanly.
        await writer._write_terminal_to_stream(
            run_id,
            chat_id=run.chat_id,
            error_text="任务已被用户取消",
            cancelled=True,
        )

    return True


def _run_kind(run: ChatRun) -> Any:
    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    return payload.get("kind")


def get_active_run_for_chat(chat_id: str, user_id: str) -> Optional[ChatRun]:
    """Backing query for GET /v1/chats/{chat_id}/active-run."""
    with engine.SessionLocal() as db:
        rows = (
            db.query(models.ChatRun)
            .filter(
                models.ChatRun.chat_id == chat_id,
                models.ChatRun.user_id == user_id,
                (
                    models.ChatRun.status.in_(settings.LIVE_STATUSES)
                    | (models.ChatRun.writer_slot.isnot(None))
                ),
            )
            .order_by(models.ChatRun.created_at.desc())
            .all()
        )
        for row in rows:
            if _run_kind(row) not in settings._BACKGROUND_RUN_KINDS:
                return row
        return None


def has_local_runs() -> bool:
    """这个进程手上有没有正在跑的 run。

    用来在查库之前便宜地排掉"本进程什么都没在跑"的情况 —— 谁在跑哪个 run 只有进程自己
    知道（``_active_runs`` 是进程内的），而有些判断必须落到具体进程上：确认条的 pending
    与前端信号队列都活在进程里，只有执行这个 run 的进程弹得出来。
    """
    return bool(tasks.process_state().active_runs)


def is_local_run(run_id: str) -> bool:
    """这个 run 是不是由本进程在执行。"""
    return run_id in tasks.process_state().active_runs


def list_active_runs_for_user(
    user_id: str, recent_seconds: int = 0
) -> Tuple[List[ChatRun], List[str]]:
    """Backing query for GET /v1/chats/active-runs, in one query.

    Returns one live run per chat: only genuinely live rows (``pending`` /
    ``running``) qualify. The per-chat probe additionally accepts a lingering
    ``writer_slot`` so a reconnecting client can clean its own zombie UI up;
    this listing feeds the sidebar's "still running" dot, where a terminal row
    must not light anything.

    With ``recent_seconds`` it also returns the chats whose runs started in that
    window, newest first, so a page finds a chat started elsewhere (Personal
    Agent, API, another device) even when its run ended between two checks.
    """
    live = models.ChatRun.status.in_(settings.LIVE_STATUSES)
    since = clock._utcnow() - _datetime.timedelta(
        seconds=min(recent_seconds, settings.MAX_RECENT_RUN_SECONDS)
    )
    with engine.SessionLocal() as db:
        rows = (
            db.query(models.ChatRun)
            .filter(
                models.ChatRun.user_id == user_id,
                (live | (models.ChatRun.created_at >= since)) if recent_seconds else live,
                # Module-owned chats (Personal Agent) never belong in the ordinary sidebar.
                reserved_chats.ordinary_chat_filter(models.ChatRun.chat_id),
            )
            .order_by(models.ChatRun.created_at.desc())
            .all()
        )
    latest_per_chat: _typing.Dict[str, models.ChatRun] = {}
    recent: _typing.Dict[str, None] = {}
    for row in rows:
        if _run_kind(row) in settings._BACKGROUND_RUN_KINDS:
            continue
        if row.status in settings.LIVE_STATUSES:
            latest_per_chat.setdefault(row.chat_id, row)
        if recent_seconds and row.created_at >= since:
            recent.setdefault(row.chat_id)
    return list(latest_per_chat.values()), list(recent)
