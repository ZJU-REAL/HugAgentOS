"""Chat Run Executor — decouples the AI workflow from the HTTP connection lifecycle.

Each sent message creates a ChatRun row + starts a background asyncio.Task. The
task consumes ``astream_chat_workflow``, converts every chunk into an SSE event
and XADDs it to the Redis Stream ``jx:chat:run:{run_id}:events``. SSE followers
read the stream via XRANGE replay + XREAD tailing, which enables "resume the
live stream after a page refresh".

Public interface:
- ``start_run(...)``          synchronously create the run + start the background worker
- ``follow_run(run_id, ...)`` SSE follower: read events from the Redis Stream
- ``cancel_run(run_id, ...)`` cancel the background task + mark status=cancelled
- ``recover_orphan_runs()``   startup hook: mark leftover running/pending runs as failed
- ``get_run(run_id)``         read the ChatRun row (used by the route layer for authz)
- ``get_active_run_for_chat(chat_id, user_id)``  probe for an in-progress run
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Literal, Optional

from redis.exceptions import TimeoutError as RedisTimeoutError

from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.db.engine import SessionLocal
from core.db.models import ChatRun
from core.infra.logging import get_logger
from core.infra.redis import get_redis
from core.services import ChatService
from orchestration.workflow import astream_chat_workflow

logger = get_logger(__name__)


# ─── Types / constants ─────────────────────────────────────────────────

RunKind = Literal["chat", "plan_execute", "plan_generate", "autonomous_loop"]

# Kinds that use cooperative cancellation (no cross-task cancel; they poll is_run_cancelled and stop themselves, avoiding the anyio cancel-scope deadlock)
_COOPERATIVE_KINDS: tuple[str, ...] = ("plan_execute", "autonomous_loop")
RunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]

_TERMINAL_STATUSES: tuple[RunStatus, ...] = ("completed", "failed", "cancelled")
_LIVE_STATUSES: tuple[RunStatus, ...] = ("pending", "running")

_STREAM_KEY = "jx:chat:run:{run_id}:events"
_STREAM_MAXLEN = 5000
_STREAM_TTL_SECONDS = 3600
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


async def _aiter_with_inactivity_timeout(aiter: AsyncIterator[Any], timeout: float):
    """Yield from an async iterator, raising ``TimeoutError`` when no item is
    produced within ``timeout`` seconds.

    Guards against a hung workflow (model loop / blocked MCP / asyncio
    deadlock) leaving the chat run stuck in 'running' forever — on timeout
    the underlying generator is closed and the error propagates into the
    existing ``except Exception`` path that writes the ``failed`` terminal.
    """
    while True:
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                await aiter.aclose()  # type: ignore[attr-defined]
            raise TimeoutError(f"工作流 {timeout:.0f}s 内无任何输出，已判定为卡死并中止") from exc
        yield item


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


def _update_run_status(run_id: str, **fields: Any) -> None:
    """Single-shot UPDATE on chat_runs; isolated session so it doesn't pollute caller's txn."""
    if not fields:
        return
    with SessionLocal() as db:
        affected = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == run_id)
            .update(fields, synchronize_session=False)
        )
        if not affected:
            logger.warning("chat_run_update_missing", run_id=run_id, fields=list(fields.keys()))
        db.commit()


def _finalize_run(run_id: str, **fields: Any) -> bool:
    """CAS variant of writing the terminal state: only updates while the run is still live.

    Prevents the race where "the reaper / cancel has already moved the run to a
    terminal state, and the worker's late completed/failed overwrites it back" —
    the side that loses the race abandons its write and returns False.
    """
    with SessionLocal() as db:
        affected = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == run_id, ChatRun.status.in_(_LIVE_STATUSES))
            .update(fields, synchronize_session=False)
        )
        db.commit()
    if not affected:
        logger.info("chat_run_finalize_skipped", run_id=run_id, fields=list(fields.keys()))
    return bool(affected)


def _create_run_record(
    *,
    chat_id: str,
    user_id: str,
    request_payload: Dict[str, Any],
) -> ChatRun:
    """Allocate run_id+message_id and INSERT a pending ChatRun row."""
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    message_id = f"msg_{uuid.uuid4().hex[:16]}"
    with SessionLocal() as db:
        run = ChatRun(
            run_id=run_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            status="pending",
            request_payload=request_payload,
            last_event_offset=0,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        # detach so caller doesn't hold a session-bound instance
        db.expunge(run)
    return run


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
    redis = get_redis()
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
        await redis.xadd(
            _stream_key(run_id),
            {"data": json.dumps(err_event, ensure_ascii=False)},
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        await redis.xadd(
            _stream_key(run_id),
            {"data": json.dumps(term_event, ensure_ascii=False)},
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        await redis.expire(_stream_key(run_id), _STREAM_TTL_SECONDS)
    except Exception as exc:
        logger.warning("chat_run_terminal_write_failed", run_id=run_id, error=str(exc))


# ─── Entry point: start_run (chat mode) ─────────────────────────────────


async def start_run(
    *,
    chat_id: str,
    user_id: str,
    session_messages: List[Dict[str, Any]],
    effective_user_message: str,
    raw_user_message: str,
    context: Dict[str, Any],
    request_payload: Dict[str, Any],
    model_name: Optional[str] = None,
) -> ChatRun:
    """Allocate run_id/message_id, INSERT chat_runs row, spawn background worker.

    Caller (chats.py) is expected to have already done auth, _ensure_chat_session,
    user-message persist, and prepared session_messages / context.
    """
    run = _create_run_record(chat_id=chat_id, user_id=user_id, request_payload=request_payload)
    _register_run_task(
        run.run_id,
        _run_workflow(
            run_id=run.run_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=run.message_id,
            session_messages=session_messages,
            effective_user_message=effective_user_message,
            raw_user_message=raw_user_message,
            context=context,
            model_name=model_name,
        ),
        name=f"chat_run:{run.run_id}",
    )
    logger.info("chat_run_started", run_id=run.run_id, chat_id=chat_id, user_id=user_id)
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


# ─── Background worker ─────────────────────────────────────────────────


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
) -> None:
    """Consume astream_chat_workflow, forward chunks to the Redis Stream, persist at the end.

    Every chunk is written to ``jx:chat:run:{run_id}:events`` via ``_emit``.
    The SSE event format stays fully identical to the existing protocol in
    chats.py:889-1087; the frontend needs no changes to its chunk parsing —
    only the new ``run_started`` event type is added.
    """
    from core.chat.tool_log import (
        attach_subagent_step,
        build_thinking_event,
        build_tool_call_event,
        build_tool_result_event,
    )
    from core.services.artifact_service import persist_artifacts as _persist_artifacts
    from core.chat.context import now_iso, resolve_user_facing_error

    _update_run_status(run_id, status="running", started_at=_utcnow())

    offset_counter = 0

    async def _emit(event: Dict[str, Any]) -> None:
        nonlocal offset_counter
        offset_counter += 1
        await _xadd_event(run_id, offset_counter, event)

    from core.llm import workspace as _workspace_mod

    full_response = ""
    metadata: Dict[str, Any] = {}
    tool_calls_log: list = []
    _workspace_mod.init_state()

    try:
        # First frame: run_started — carries run_id / message_id; the frontend uses these to resume / cancel
        await _emit(
            {
                "type": "run_started",
                "run_id": run_id,
                "message_id": message_id,
                "chat_id": chat_id,
            }
        )

        # Compaction notice (mirrors Codex's post-compaction Warning): after the
        # previous turn's stream closed, a new compaction checkpoint was written
        # in the background → notify the user once in this turn's first frame.
        # Failures are silent and must never affect the main conversation.
        try:
            from core.services.compaction_service import pop_compaction_notice

            with SessionLocal() as _cn_db:
                _notify_compaction = pop_compaction_notice(ChatService(_cn_db), chat_id)
            if _notify_compaction:
                await _emit({"type": "compaction_notice", "chat_id": chat_id})
        except Exception as _cn_exc:  # noqa: BLE001
            logger.debug("compaction_notice_failed", chat_id=chat_id, error=str(_cn_exc))

        # When the agent kicks off a batch_plan flow we want to suppress
        # follow-up question generation for THIS turn — the assistant's
        # message body is empty (just the batch_plan tool call), so the
        # follow-ups would either be nonsense or anchor to the user's
        # original prompt instead of the upcoming batch results.
        seen_batch_confirm = False

        async for chunk in _aiter_with_inactivity_timeout(
            astream_chat_workflow(
                session_messages=session_messages,
                user_message=effective_user_message,
                context=context,
            ),
            _INACTIVITY_TIMEOUT_SEC,
        ):
            chunk_type = chunk.get("type")

            if chunk_type == "thinking":
                await _emit(build_thinking_event(chunk, chat_id))

            elif chunk_type in {"ai_message", "content"}:
                delta = chunk.get("delta", "")
                if delta:
                    full_response += delta
                    await _emit(
                        {
                            "type": "content",
                            "event": "ai_message",
                            "delta": delta,
                            "chat_id": chat_id,
                        }
                    )

            elif chunk_type == "tool_call":
                await _emit(build_tool_call_event(chunk, chat_id, tool_calls_log))

            elif chunk_type == "tool_result":
                await _emit(build_tool_result_event(chunk, chat_id, tool_calls_log))

            elif chunk_type == "heartbeat":
                # Not written to the stream — the SSE layer's own keep-alive cadence is handled by Nginx/StreamingResponse
                continue

            elif chunk_type == "tool_pending":
                await _emit(
                    {
                        "type": "tool_pending",
                        "chat_id": chat_id,
                        "reason": chunk.get("reason", "llm_buffering"),
                    }
                )

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
                        tool_calls_log, str(chunk.get("parent_tool_id", "") or ""), chunk
                    )
                except Exception:
                    logger.debug("attach_subagent_step failed (ignored)", exc_info=True)
                chunk["chat_id"] = chat_id
                await _emit(chunk)

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

            elif chunk_type == "plan_redirect":
                # The main agent called enter_plan_mode to switch into plan mode —
                # workflow.py has already broken out of the current agent loop.
                # Forward the to-be-planned task to the frontend, which drives the
                # existing plan-mode pipeline (generate plan → preview card → user
                # confirms → execute). Same human-in-the-loop gate as
                # batch_confirm: the agent does not continue on its own; the user
                # confirms on the plan card.
                await _emit(
                    {
                        "type": "plan_redirect",
                        "chat_id": chat_id,
                        "task_description": chunk.get("task_description", ""),
                    }
                )

            elif chunk_type == "meta":
                # Strict workspace gate: pinned list is the sole source of
                # user-visible artifacts. See chats.py:_stream_sse_response.
                _ws_pinned = _workspace_mod.get_pinned()
                _ws_files = _workspace_mod.get_pinned_file_ids()
                metadata = {
                    "type": "meta",
                    "route": chunk.get("route", "main"),
                    "sources": chunk.get("sources", []),
                    "artifacts": _ws_pinned,
                    "warnings": chunk.get("warnings", []),
                    "is_markdown": chunk.get("is_markdown", False),
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "citations": chunk.get("citations", []),
                    "workspace_files": _ws_files,
                }
                await _emit(metadata)

                # Persist: assistant message + artifacts — separate session
                usage_payload = chunk.get("usage") or None
                _persist_extra = {
                    "timestamp": now_iso(),
                    "route": metadata.get("route"),
                    "is_markdown": metadata.get("is_markdown", False),
                    "sources": metadata.get("sources", []),
                    "artifacts": metadata.get("artifacts", []),
                    "warnings": metadata.get("warnings", []),
                    "citations": metadata.get("citations", []),
                    "message_id": message_id,
                    "workspace_files": _ws_files,
                }
                if context.get("model_provider_id"):
                    _persist_extra["model_provider_id"] = context.get("model_provider_id")
                with SessionLocal() as db:
                    chat_service = ChatService(db)
                    chat_service.add_message(
                        chat_id=chat_id,
                        role="assistant",
                        content=full_response,
                        model=model_name,
                        tool_calls=tool_calls_log if tool_calls_log else None,
                        usage=usage_payload,
                        message_id=message_id,
                        extra_data=_persist_extra,
                    )
                    # Build a ProjectScope from the workflow context and pass it
                    # explicitly: in a team project, without this line →
                    # scope=None → pinned files are treated as a non-project chat
                    # and written as orphan rows with user_folder_id=NULL/
                    # team_id=NULL, leaking into the personal MySpace root.
                    from core.services.project_scope import project_scope_from_context

                    _persist_artifacts(
                        db,
                        user_id,
                        chat_id,
                        _ws_pinned,
                        scope=project_scope_from_context(context),
                    )

                _finalize_run(
                    run_id,
                    status="completed",
                    usage=usage_payload,
                    completed_at=_utcnow(),
                    last_event_offset=offset_counter,
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
                        chat_id=chat_id,
                        user_msg=raw_user_message,
                        response=full_response,
                        msg_id=message_id,
                    )

                # End-of-turn compaction: when real token usage crosses the
                # threshold → generate a summary checkpoint in the background;
                # doesn't block stream close, and failures don't affect the main
                # conversation.
                _spawn_compaction_task(
                    chat_id=chat_id,
                    model_name=model_name,
                    usage=usage_payload,
                )

        # Workflow returned normally (with or without a meta event): write the terminal marker
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id})

    except asyncio.CancelledError:
        # Triggered by cancel_run: write a user-cancelled event + terminal marker so followers exit gracefully
        logger.info("chat_run_cancelled", run_id=run_id)
        await _emit(
            {
                "type": "error",
                "error": "任务已被用户取消",
                "delta": "任务已被用户取消",
                "chat_id": chat_id,
                "_cancelled": True,
            }
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True})
        # status was already updated in cancel_run(); here we only make sure last_event_offset is in sync
        _update_run_status(run_id, last_event_offset=offset_counter)

    except Exception as exc:
        logger.error("chat_run_failed", run_id=run_id, error=str(exc), exc_info=True)
        try:
            user_facing = resolve_user_facing_error(exc)
        except Exception:
            user_facing = "请求处理失败，请稍后重试"
        # Fallback: persist an empty assistant message + error, matching the existing frontend parsing path
        try:
            with SessionLocal() as db:
                ChatService(db).add_message(
                    chat_id=chat_id,
                    role="assistant",
                    content="",
                    model=model_name,
                    message_id=message_id,
                    error={"error": str(exc), "timestamp": _utcnow().isoformat()},
                )
        except Exception:
            pass
        _finalize_run(
            run_id,
            status="failed",
            error_message=str(exc)[:1000],
            completed_at=_utcnow(),
            last_event_offset=offset_counter,
        )
        await _emit(
            {
                "type": "error",
                "error": user_facing,
                "delta": user_facing,
                "chat_id": chat_id,
            }
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id})

    finally:
        # After the terminal state, extend the stream's TTL by 1h so late resume requests can still fetch the full history
        await _expire_stream(run_id)


def _spawn_compaction_task(*, chat_id: str, model_name: str, usage: Optional[Dict]) -> None:
    """Trigger end-of-turn context compaction (fire-and-forget; never affects the main conversation)."""
    from core.config.settings import settings as _settings

    if not _settings.compaction.enabled:
        return

    async def _bg() -> None:
        try:
            from core.llm.context_manager import resolve_model_context_window
            from core.services.compaction_service import (
                resolve_active_tokens,
                resolve_token_limit,
                run_post_turn_compaction,
                should_compact,
            )

            # Trigger criterion — see resolve_active_tokens: real end-of-turn context occupancy, not the cumulative billing value
            active_tokens = resolve_active_tokens(usage)
            limit = resolve_token_limit(resolve_model_context_window(model_name or ""))
            if not should_compact(active_tokens, limit):
                return
            logger.info(
                "chat_compaction_triggered", chat_id=chat_id,
                active_tokens=active_tokens, limit=limit,
            )
            await run_post_turn_compaction(chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("background_compaction_failed", error=str(exc))

    try:
        asyncio.create_task(_bg())
    except RuntimeError:
        pass


def _spawn_followup_task(*, chat_id: str, user_msg: str, response: str, msg_id: str) -> None:
    """Equivalent to the existing _generate_followups_bg in chats.py."""
    from core.llm.message_compat import strip_thinking
    from orchestration.followups import get_followup_generator

    async def _bg() -> None:
        try:
            clean_resp = strip_thinking(response)
            questions = await asyncio.wait_for(
                get_followup_generator().generate(user_msg, clean_resp),
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
    """
    redis = get_redis()
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
    """Decode the fields of a single Redis Stream entry into an SSE event dict."""
    if not fields:
        return None
    raw = fields.get("data") if isinstance(fields, dict) else None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


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
    run = _create_run_record(chat_id=chat_id, user_id=user_id, request_payload=request_payload)
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
        ),
        name=f"plan_run:{run.run_id}",
    )
    logger.info(
        "plan_run_started", run_id=run.run_id, plan_id=plan_id, chat_id=chat_id, user_id=user_id
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
) -> None:
    """Background plan-execute worker. Streams via XADD, persists at end.

    Uses short-lived sessions (one per logical operation) to avoid holding a
    DB connection across the entire long-running stream.
    """
    from orchestration.subagents.plan_mode import astream_execute_plan
    from core.services.plan_service import PlanService
    from core.chat.tool_log import attach_tool_result as _attach_tool_result
    from core.services.artifact_service import persist_artifacts as _persist_artifacts
    from core.llm import workspace as _workspace_mod

    _update_run_status(run_id, status="running", started_at=_utcnow())

    offset_counter = 0

    async def _emit(event: Dict[str, Any]) -> None:
        nonlocal offset_counter
        offset_counter += 1
        await _xadd_event(run_id, offset_counter, event)

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

        # Persist assistant message + artifacts in another short session
        with SessionLocal() as persist_db:
            chat_service = ChatService(persist_db)
            content = (
                result_text or f"计划执行完成：共 {total_steps} 步，完成 {completed_steps} 步。"
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
            )

        _finalize_run(
            run_id,
            status="completed",
            usage=exec_usage,
            completed_at=_utcnow(),
            last_event_offset=offset_counter,
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id})

    except asyncio.CancelledError:
        logger.info("plan_run_cancelled", run_id=run_id)
        await _emit(
            {
                "type": "plan_error",
                "plan_id": plan_id,
                "error": "任务已被用户取消",
                "_cancelled": True,
            }
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True})
        _update_run_status(run_id, last_event_offset=offset_counter)

    except Exception as exc:
        logger.error("plan_run_failed", run_id=run_id, error=str(exc), exc_info=True)
        _finalize_run(
            run_id,
            status="failed",
            error_message=str(exc)[:1000],
            completed_at=_utcnow(),
            last_event_offset=offset_counter,
        )
        await _emit({"type": "plan_error", "plan_id": plan_id, "error": str(exc)[:200]})
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id})

    finally:
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
    evaluator_model: Optional[str] = None,
    worker_max_iters: int = 15,
    hitl_enabled: bool = False,
    enable_thinking: bool = False,
    chat_mode: Optional[str] = None,
    is_resume: bool = False,
    project_id: Optional[str] = None,
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
    }
    run = _create_run_record(chat_id=chat_id, user_id=user_id, request_payload=request_payload)
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
            evaluator_model=evaluator_model,
            worker_max_iters=worker_max_iters,
            hitl_enabled=hitl_enabled,
            enable_thinking=enable_thinking,
            chat_mode=chat_mode,
            is_resume=is_resume,
            project_id=project_id,
        ),
        name=f"autoloop_run:{run.run_id}",
    )
    logger.info("autonomous_loop_run_started", run_id=run.run_id, loop_id=loop_id, user_id=user_id)
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
    evaluator_model: Optional[str],
    worker_max_iters: int,
    hitl_enabled: bool = False,
    enable_thinking: bool = False,
    chat_mode: Optional[str] = None,
    is_resume: bool = False,
    project_id: Optional[str] = None,
) -> None:
    from orchestration.autonomous_loop import LoopBudget, run_autonomous_loop
    from orchestration.loop_evaluator import GoalSpec

    _update_run_status(run_id, status="running", started_at=_utcnow())
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

    async def _emit(event: Dict[str, Any]) -> None:
        nonlocal offset_counter
        et = event.get("type")
        if et == "content":
            _delta = event.get("delta")
            if _delta:
                content_buf.append(str(_delta))
        elif et == "tool_call":
            _tid = str(event.get("tool_id") or f"t{len(tool_log)}")
            tool_idx[_tid] = len(tool_log)
            tool_log.append({
                "id": _tid, "name": event.get("tool_name") or "tool",
                "input": event.get("tool_args"), "status": "running",
            })
        elif et == "tool_result":
            _i = tool_idx.get(str(event.get("tool_id") or ""))
            if _i is not None:
                tool_log[_i]["output"] = event.get("result")
                tool_log[_i]["status"] = "error" if event.get("error") else "success"
        await _xadd_event(run_id, offset_counter, event)
        offset_counter += 1
        # At structural checkpoints (requirement flipped / per-iteration
        # evaluation done) persist progress incrementally — so after a mid-run
        # crash/restart/refresh the already-produced body + tool cards are still
        # visible from the DB, rather than waiting for a single write at the
        # terminal state (see the fix for symptoms 2/3).
        if et in ("requirement_passed", "iteration_evaluated"):
            _flush_loop_message()

    def _flush_loop_message(status: str = "running") -> None:
        """Upsert the currently accumulated worker body + tool cards into the assistant message (same message_id).

        Conversational mode (self_verify) only. Skipped when output is empty
        (no body and no tools yet) to avoid writing an empty bubble.
        """
        if not conversational:
            return
        body = "".join(content_buf).strip()
        if not body and not tool_log:
            return
        try:
            with SessionLocal() as db:
                ChatService(db).upsert_message(
                    chat_id=chat_id, role="assistant", content=body,
                    message_id=message_id,
                    tool_calls=tool_log if tool_log else None,
                    extra_data={"autonomous_loop": True, "loop_id": loop_id,
                                "loop_status": status},
                )
        except Exception:  # noqa: BLE001 - incremental persist failure must not take down the run
            logger.warning("loop incremental persist failed", exc_info=True)

    gs = GoalSpec(
        objective=goal_spec.get("objective", ""),
        acceptance_criteria=goal_spec.get("acceptance_criteria", []) or [],
    )
    bud = LoopBudget(
        max_iters=int(budget.get("max_iters", 50)),
        max_wall_clock_s=float(budget.get("max_wall_clock_s", 6 * 3600)),
        max_tokens=int(budget.get("max_tokens", 10_000_000)),
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
    if project_id:
        try:
            from core.services.project_scope import build_project_ctx
            with SessionLocal() as db:
                project_ctx = build_project_ctx(db, project_id)
                if project_ctx:
                    # Bind the loop session to the project (publish_site's internal API reverse-looks-up via ChatSession.project_id).
                    from core.db.models import ChatSession
                    sess = db.query(ChatSession).filter(ChatSession.chat_id == chat_id).first()
                    if sess is not None and sess.project_id != project_id:
                        sess.project_id = project_id
                        db.commit()
        except Exception:  # noqa: BLE001 - project resolution failure degrades to the isolated sandbox; must not take down the loop
            logger.warning("loop project_ctx resolve failed", exc_info=True)
            project_ctx = None
    session_id = chat_id if project_ctx else f"loop-{loop_id}"
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
        with SessionLocal() as db:
            _LoopSvc(db).save_ledger(loop_id, led)

    try:
        await _emit({
            "type": "run_started", "run_id": run_id, "message_id": message_id,
            "chat_id": chat_id, "kind": "autonomous_loop", "loop_id": loop_id,
        })
        if conversational and gs.objective and not is_resume:
            # Only the first start persists the objective as a user message; "continue" (resume) does not insert it again.
            try:
                with SessionLocal() as db:
                    ChatService(db).add_message(
                        chat_id=chat_id, role="user", content=gs.objective,
                        extra_data={"autonomous_loop": True, "loop_id": loop_id},
                    )
            except Exception:  # noqa: BLE001
                logger.warning("loop user-msg persist failed", exc_info=True)
        try:
            from core.services.loop_service import LoopService
            with SessionLocal() as db:
                LoopService(db).mark_running(loop_id, workspace_session=session_id)
        except Exception:  # noqa: BLE001 - audit is non-critical; don't block execution
            logger.warning("loop mark_running failed", exc_info=True)

        result = await run_autonomous_loop(
            loop_id=loop_id, user_id=user_id, goal_spec=gs, budget=bud,
            model_name=model_name, evaluator_model=evaluator_model or "fast",
            worker_max_iters=worker_max_iters, session_id=session_id,
            hitl_enabled=hitl_enabled, enable_thinking=enable_thinking,
            chat_mode=chat_mode,
            emit=_emit, is_cancelled=lambda: is_run_cancelled(run_id),
            load_ledger=_load_ledger, save_ledger=_save_ledger,
            project_ctx=project_ctx, chat_id=chat_id,
        )

        try:
            from core.services.loop_service import LoopService
            with SessionLocal() as db:
                LoopService(db).persist_result(loop_id, result)
        except Exception:  # noqa: BLE001
            logger.warning("loop persist_result failed", exc_info=True)

        if conversational:
            try:
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
                with SessionLocal() as db:
                    # upsert: this assistant message was already created during
                    # incremental persistence (same message_id); at the terminal
                    # state overwrite it with the final version carrying the
                    # status footer — no duplicate rows.
                    ChatService(db).upsert_message(
                        chat_id=chat_id, role="assistant",
                        content=_asst_content,
                        usage={"total_tokens": result.tokens_spent},
                        tool_calls=tool_log if tool_log else None,
                        extra_data={"autonomous_loop": True, "loop_id": loop_id,
                                    "loop_status": result.status},
                        message_id=message_id,
                    )
            except Exception:  # noqa: BLE001
                logger.warning("loop assistant-msg persist failed", exc_info=True)

        run_status = (
            "completed" if result.status in ("completed", "budget_exhausted", "awaiting_human")
            else ("cancelled" if result.status == "cancelled" else "failed")
        )
        _finalize_run(
            run_id, status=run_status, usage={"total_tokens": result.tokens_spent},
            completed_at=_utcnow(), last_event_offset=offset_counter,
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id})
    except asyncio.CancelledError:
        # Cancelled: run_autonomous_loop was interrupted and no result is
        # available, so the terminal-state persistence block is skipped — here
        # we persist the progress produced so far (symptoms 2/3); otherwise a
        # reopened chat would only contain the user's objective.
        _flush_loop_message("cancelled")
        await _emit({"type": "loop_error", "error": "任务已被用户取消", "_cancelled": True})
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True})
        _update_run_status(run_id, last_event_offset=offset_counter)
    except Exception as exc:  # noqa: BLE001
        logger.exception("autonomous_loop_run_failed", run_id=run_id)
        _flush_loop_message("failed")  # persist partial progress before crashing
        _finalize_run(
            run_id, status="failed", error_message=str(exc)[:1000],
            completed_at=_utcnow(), last_event_offset=offset_counter,
        )
        await _emit({"type": "loop_error", "error": str(exc)[:500]})
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id})
    finally:
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
    run = _create_run_record(chat_id=chat_id, user_id=user_id, request_payload=request_payload)
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
) -> None:
    from orchestration.subagents.plan_mode import astream_generate_plan

    _update_run_status(run_id, status="running", started_at=_utcnow())
    offset_counter = 0

    async def _emit(event: Dict[str, Any]) -> None:
        nonlocal offset_counter
        offset_counter += 1
        await _xadd_event(run_id, offset_counter, event)

    plan_id_out: Optional[str] = None
    plan_title = ""
    plan_desc = ""
    plan_snapshot: Optional[Dict[str, Any]] = None
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
                    plan_id_out = event.get("plan_id")
                    plan_title = event.get("title", "")
                    plan_desc = event.get("description", "")
                    steps = event.get("steps", [])
                    step_summary = "\n".join(
                        f"{i+1}. {s.get('title', '')}" for i, s in enumerate(steps)
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

        if assistant_content and plan_id_out:
            with SessionLocal() as persist_db:
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
                )

        _finalize_run(
            run_id,
            status="completed",
            usage=gen_usage,
            completed_at=_utcnow(),
            last_event_offset=offset_counter,
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id})

    except asyncio.CancelledError:
        logger.info("plan_generate_run_cancelled", run_id=run_id)
        await _emit(
            {
                "type": "plan_error",
                "error": "任务已被用户取消",
                "_cancelled": True,
            }
        )
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True})
        _update_run_status(run_id, last_event_offset=offset_counter)

    except Exception as exc:
        logger.error("plan_generate_run_failed", run_id=run_id, error=str(exc), exc_info=True)
        _finalize_run(
            run_id,
            status="failed",
            error_message=str(exc)[:1000],
            completed_at=_utcnow(),
            last_event_offset=offset_counter,
        )
        await _emit({"type": "plan_error", "error": str(exc)[:200]})
        await _emit({"type": _TERMINAL_TYPE, "chat_id": chat_id})

    finally:
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

    if not _finalize_run(run_id, status="cancelled", completed_at=_utcnow()):
        # Race: the worker/reaper just beat us to writing the terminal state — treat as "no longer running".
        return False

    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    cooperative_only = payload.get("kind") in _COOPERATIVE_KINDS

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
    else:
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
    """Mark live runs left over from a crashed/restarted process as failed.

    Bulk-update by status filter; then write terminal markers to each stream so
    any follower waiting on Redis doesn't hang.
    """
    with SessionLocal() as db:
        orphans = (
            db.query(ChatRun.run_id, ChatRun.chat_id)
            .filter(ChatRun.status.in_(_LIVE_STATUSES))
            .all()
        )
        if not orphans:
            return 0
        run_chat_pairs = [(r.run_id, r.chat_id) for r in orphans]
        db.query(ChatRun).filter(ChatRun.status.in_(_LIVE_STATUSES)).update(
            {
                "status": "failed",
                "error_message": "server restarted before run completed",
                "completed_at": _utcnow(),
            },
            synchronize_session=False,
        )
        db.commit()

    for rid, cid in run_chat_pairs:
        await _write_terminal_to_stream(
            rid,
            chat_id=cid or "",
            error_text="服务重启导致任务中断，请重新发起",
        )
    logger.info("chat_run_orphan_recovered", count=len(run_chat_pairs))
    return len(run_chat_pairs)


async def resume_running_loops() -> int:
    """At startup, resume autonomous loops interrupted by a crash/restart (M4 checkpoint resume).

    The persistent sandbox files (feature_list.json/handoffs.md) still exist
    (same session) → re-invoke start_autonomous_loop_run with the same loop_id,
    and the driver automatically resumes from feature_list.json. Only orphan
    loops with status='running' are resumed; 'awaiting_human'/terminal states
    are left alone. Off by default (LOOP_AUTO_RESUME=false) to avoid accidental
    re-runs in shared environments.
    """
    if os.getenv("LOOP_AUTO_RESUME", "false").strip().lower() not in ("1", "true", "yes"):
        return 0
    from core.db.models import AgentLoop

    resumed = 0
    with SessionLocal() as db:
        loops = db.query(AgentLoop).filter(AgentLoop.status == "running").all()
        specs = [
            (x.loop_id, x.chat_id, x.user_id, dict(x.goal_spec or {}), dict(x.budget or {}),
             (x.extra_data or {}).get("project_id"))
            for x in loops
        ]
    for loop_id, chat_id, user_id, goal_spec, budget, project_id in specs:
        if not chat_id:
            continue
        try:
            await start_autonomous_loop_run(
                loop_id=loop_id, chat_id=chat_id, user_id=user_id,
                goal_spec=goal_spec, budget=budget, project_id=project_id,
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
      unconditionally, preventing a runaway agent loop from living forever by
      continuously emitting.

    Reaping also cancels the in-process worker task, keeping the DB terminal
    state aligned with the actual task (historical bug: only flipping the DB
    without killing the task let the worker silently run another 1.5h and
    overwrite the terminal state back to completed). plan_execute is the
    exception — cross-task cancel risks the anyio cancel-scope deadlock (see the
    ``cancel_run`` docstring); it stops itself after ``is_run_cancelled``
    polling observes the terminal state.

    Complements ``recover_orphan_runs`` (startup-only) and the per-run
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
        began_at = row.began_at
        if began_at is not None and began_at.tzinfo is None:  # SQLite stores naive UTC
            began_at = began_at.replace(tzinfo=timezone.utc)
        hard_expired = began_at is not None and began_at < hard_cutoff

        if not hard_expired:
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

        if not _finalize_run(rid, status="failed", error_message=reason, completed_at=_utcnow()):
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
        reaped += 1

    if reaped:
        logger.info("chat_run_stale_reaped", count=reaped)
    return reaped


async def run_stale_reaper_loop() -> None:
    """Background loop started once at app startup; never returns normally."""
    while True:
        try:
            await asyncio.sleep(_STALE_REAPER_INTERVAL_SEC)
            await reap_stale_runs()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("chat_run_stale_reaper_iteration_failed", exc_info=True)


def get_active_run_for_chat(chat_id: str, user_id: str) -> Optional[ChatRun]:
    """Backing query for GET /v1/chats/{chat_id}/active-run."""
    with SessionLocal() as db:
        return (
            db.query(ChatRun)
            .filter(
                ChatRun.chat_id == chat_id,
                ChatRun.user_id == user_id,
                ChatRun.status.in_(_LIVE_STATUSES),
            )
            .order_by(ChatRun.created_at.desc())
            .first()
        )


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
