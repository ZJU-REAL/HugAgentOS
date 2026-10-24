"""Own one chat worker lifecycle and coordinate its persistence phases."""

from __future__ import annotations
from core.runs.tool_effects import errors

import asyncio
import contextlib
import datetime as _datetime
import time
import typing as _typing
from typing import Any, Dict, List, Optional

from core.harness.registry.chat_execution import stream_chat_workflow
from core.db.threads import run_in_session
import core.harness.hooks as hooks
import core.llm.runtime.model_steps as model_steps
import core.runs.clock as clock
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.runtime as runtime
import core.runs.settings as settings
import core.runs.watchdog as watchdog
import core.chat.service as services_ChatService_module
import core.runs.journal as run_journal
from core.infra.logging import get_logger


from core.chat.runtime import chunks, pauses, snapshots
from core.chat.runtime.turn_state import TurnState

logger = get_logger(__name__)


def _pop_compaction_notice(db, chat_id: str) -> Optional[Dict[str, Any]]:
    """Consume a pending compaction notice; return the state to show, or None."""
    from core.chat.compaction.service import pop_compaction_notice
    from core.chat.compaction.usage_projection import get_chat_context_state

    chat_service = services_ChatService_module.ChatService(db)
    if not pop_compaction_notice(chat_service, chat_id):
        return None
    return get_chat_context_state(chat_service, chat_id)["context_compaction"]


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
    turn = TurnState(
        chat_id=chat_id,
        context=context,
        message_id=message_id,
        model_name=model_name,
        run_id=run_id,
        user_id=user_id,
    )
    from core.chat.context import resolve_user_facing_error
    from core.chat.tool_log import SegmentRecorder

    # 本轮的墙钟起点。认领这一轮的 worker 手里就有它；只有接管别人未完成的
    # 那一轮时才需要回库里取（见 _run_started_at_ms）。
    claimed_at: _typing.Optional[_datetime.datetime] = None
    initial_claimed = False
    if not recovering:
        claimed_at = ownership._claim_run_execution(turn.run_id)
        initial_claimed = claimed_at is not None
        if not initial_claimed:
            ownership._acknowledge_never_started_terminal_writer(turn.run_id)
            return

    turn.owner = journal_owner or ownership.new_worker_owner(turn.run_id)
    turn.journal = ownership.journal()
    if not turn.journal.claim(
        turn.run_id, owner=turn.owner, lease_seconds=settings.RUN_LEASE_SECONDS
    ):
        logger.info("chat_run_claim_skipped", run_id=turn.run_id, owner=turn.owner)
        if initial_claimed:
            ownership._acknowledge_terminal_writer(turn.run_id)
        return
    try:
        turn.journal.append_operation(
            turn.run_id,
            owner=turn.owner,
            operation_type="worker_started",
            phase="pre_model",
            safety="replayable",
        )
    except run_journal.RunLeaseLost:
        logger.warning("chat_run_worker_fenced_before_start", run_id=turn.run_id, owner=turn.owner)
        return
    worker_task = asyncio.current_task()
    if worker_task is None:  # pragma: no cover - asyncio always owns this coroutine
        raise RuntimeError("chat run worker has no asyncio task")
    lease_lost = asyncio.Event()
    turn.lease_task = asyncio.create_task(
        ownership.lease_heartbeat(turn.run_id, turn.owner, worker_task, lease_lost),
        name=f"chat_run_lease:{turn.run_id}",
    )
    # 本轮回答总耗时起点 —— 持久化进 extra_data.duration_ms，历史加载后「用时」不再消失
    turn.run_started_monotonic = time.monotonic()

    turn.offsets = writer.EventOffsets(
        turn.journal, turn.run_id, turn.owner, lease_lost=lease_lost
    )

    turn.emit = turn.offsets.emit

    from core.llm.runtime import workspace as _workspace_mod

    turn.full_response = ""
    # A steer starts a new visible assistant segment in the same backend run.
    # Each segment gets its own message id so persisted history stays in the
    # same chronological order the user saw while streaming.
    turn.current_message_id = turn.message_id
    turn.current_chat_seq = assistant_chat_seq
    turn.latest_user_message = raw_user_message
    turn.metadata: _typing.Dict[str, _typing.Any] = {}
    turn.compaction_budget_inputs: _typing.Dict[str, _typing.Any] = {}
    turn.tool_calls_log: list = []
    _workspace_mod.init_state()
    # 结构化 reasoning 通道（deepseek 系 reasoning_content/reasoning）的思考增量。
    # 落进独立的 thinking 列，不再拼进 full_response —— 拼进正文会把正文顶出
    # content 的 10 万字上限，截断切掉闭合标签后整段思考会漏成正文。
    # 每个思考块记录它出现时的正文偏移，历史重建按此原位还原。
    turn.thinking_parts: _typing.List[str] = []
    turn.thinking_log: _typing.List[_typing.Dict[str, _typing.Any]] = []
    # 模型回放用的规范记录：每个模型响应、每个工具结果按发生顺序各记一条
    # （core/llm/runtime/model_steps.py）。上面的正文 / 思考 / 工具卡是给界面看的投影，
    # 下一轮发给模型的历史从这一份恢复。
    turn.model_steps: _typing.List[_typing.Dict[str, _typing.Any]] = []
    # 正文 / 思考 / 工具卡片的先后，在它们产生的那一刻就记下来，落进
    # metadata.segments；刷新后照着渲染，不做任何反推。
    turn.segments = SegmentRecorder()
    # 本轮已开的思考块；跨轮（工具调用边界）置 None 强制另起一块。不设边界的话，
    # 正文一旦出现，后面每一轮的思考都会并进同一块里无限膨胀。
    turn.round_block: _typing.Optional[_typing.Dict[str, _typing.Any]] = None
    next_cancel_poll = 0.0

    # In-flight persistence. The assistant row was created at admission and is
    # refreshed with this turn's display state — right away at tool boundaries,
    # and by the checkpoint task every couple of seconds while text streams —
    # so the server holds the turn at all times, not only at the end. Each
    # refresh is one lease-guarded UPDATE off the event loop; the lock keeps a
    # boundary flush from racing the periodic one with an older snapshot.
    turn.flush_lock = asyncio.Lock()
    turn.flushed_snapshot_key = None
    turn.inflight_interval_s = 2.0
    checkpoint_task: _typing.Optional[asyncio.Task] = None

    try:
        if recovering:
            await writer._reset_stream_projection(turn.run_id)
        # First frame: run_started — carries run_id / message_id; the frontend uses these to resume / cancel
        await turn.emit(
            {
                "type": "run_started",
                "run_id": turn.run_id,
                "message_id": turn.message_id,
                "chat_id": turn.chat_id,
                "started_at": (
                    clock._epoch_ms(claimed_at)
                    if claimed_at
                    else clock._run_started_at_ms(turn.run_id)
                ),
            }
        )
        if recovering:
            await turn.emit(
                {
                    "type": "content_replace",
                    "content": "",
                    "reason": "run_recovered",
                    "chat_id": turn.chat_id,
                }
            )

        # Compaction notice (mirrors Codex's post-compaction Warning): after the
        # previous turn's stream closed, a new compaction checkpoint was written
        # in the background → notify the user once in this turn's first frame.
        # Failures are silent and must never affect the main conversation.
        try:
            _compaction_state = await run_in_session(_pop_compaction_notice, turn.chat_id)
            if _compaction_state is not None:
                await turn.emit(
                    {
                        "type": "compaction_notice",
                        "chat_id": turn.chat_id,
                        "context_compaction": _compaction_state,
                    }
                )
        except Exception as _cn_exc:  # noqa: BLE001
            logger.debug("compaction_notice_failed", chat_id=turn.chat_id, error=str(_cn_exc))

        # Evidence-plane join keys (GCE ticket 04). Injected here rather than at
        # every context construction site: this is the one place that owns both
        # the run and its pre-allocated assistant message id.
        turn.context["run_id"] = turn.run_id
        turn.context["message_id"] = turn.message_id
        turn.context["journal_owner"] = turn.owner

        # Stamp the message id onto tool logging for this run. The plumbing
        # already existed but had no caller, so every tool call was written with
        # an empty message_id — which silently broke the join the evidence plane
        # depends on, and left every episode with no tool sequence to learn from.
        try:
            from core.harness.log_context import set_current_message_id

            set_current_message_id(turn.message_id)
        except Exception as fallback_error:  # pragma: no cover - logging must never fail a run
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.chat.runtime.worker:_run_workflow', fallback_error)

        checkpoint_task = asyncio.create_task(snapshots._checkpoint_loop(turn))
        async for chunk in watchdog._aiter_with_inactivity_timeout(
            stream_chat_workflow(
                raw_user_message=raw_user_message,
                session_messages=session_messages,
                user_message=effective_user_message,
                context=turn.context,
            ),
            settings._INACTIVITY_TIMEOUT_SEC,
            is_activity=lambda item: watchdog._is_chat_run_activity(item, turn.chat_id),
            first_response_timeout=settings._FIRST_RESPONSE_TIMEOUT_SEC,
        ):
            now = time.monotonic()
            if now >= next_cancel_poll:
                next_cancel_poll = now + settings._CANCEL_VISIBILITY_SEC
                if runtime.is_run_cancelled(turn.run_id):
                    raise asyncio.CancelledError
            await chunks.consume_chunk(turn, chunk)
            if turn.stopped:
                return

        # AgentScope may absorb a tool adapter exception after ToolEffectMiddleware
        # has already durably paused the run. Re-read the DB before projecting a
        # normal terminal marker; otherwise allocate_event_offset correctly
        # rejects the non-completed run and the outer handler overwrites
        # ``needs_attention`` with ``failed``.
        try:
            current = runtime.get_run(turn.run_id)
        except AttributeError:
            # Some narrow unit adapters replace SessionLocal with a commit-only
            # fake that intentionally has no query surface.
            current = None
        if current is not None:
            if current.status == "needs_attention":
                await pauses._pause_for_tool_outcome(
                    turn, RuntimeError(current.failure_reason or "tool outcome requires recovery")
                )
                return
            if current.status in settings._TERMINAL_STATUSES and current.status != "completed":
                raise asyncio.CancelledError

        # Workflow returned normally (with or without a meta event): write the terminal marker
        await turn.emit({"type": settings.TERMINAL_TYPE, "chat_id": turn.chat_id}, terminal=True)

    except asyncio.CancelledError:
        if lease_lost.is_set():
            # Another process owns recovery now. The fenced worker must not
            # project a user cancellation or mutate the durable row. The one
            # exception is fencing caused by the user's own cancel: nobody will
            # take that run over, so the partial answer must be persisted here.
            logger.warning("chat_run_worker_fenced", run_id=turn.run_id, owner=turn.owner)
            if ownership.cancelled_by_user(turn.run_id):
                snapshots._persist_cancelled_partial(turn)
        else:
            current = runtime.get_run(turn.run_id)
            if current is None or current.status != "cancelled":
                # Reaper/hard-cap/external recovery may cancel the local task
                # after publishing its own authoritative terminal verdict.
                logger.warning(
                    "chat_run_worker_stopped_by_external_terminal",
                    run_id=turn.run_id,
                    status=(current.status if current is not None else None),
                )
            else:
                # Triggered by cancel_run: persist what was already produced,
                # then write a user-cancelled event + terminal marker so
                # followers exit gracefully.
                logger.info("chat_run_cancelled", run_id=turn.run_id)
                snapshots._persist_cancelled_partial(turn)
                await turn.emit(
                    {
                        "type": "error",
                        "error": "任务已被用户取消",
                        "delta": "任务已被用户取消",
                        "chat_id": turn.chat_id,
                        "_cancelled": True,
                    },
                    terminal=True,
                )
                await turn.emit(
                    {"type": settings.TERMINAL_TYPE, "chat_id": turn.chat_id, "_cancelled": True},
                    terminal=True,
                )

    except errors.ToolOutcomeUnknown as exc:
        await pauses._pause_for_tool_outcome(turn, exc)

    except hooks.HookPaused as exc:
        await pauses._pause_for_hook(turn, exc)

    except run_journal.RunLeaseLost:
        # A takeover/cancel may race with any journal append. Once fenced, the
        # old worker must not persist fallback messages or publish a terminal —
        # except when the fencing came from the user's own cancel.
        logger.warning("chat_run_worker_fenced", run_id=turn.run_id, owner=turn.owner)
        if ownership.cancelled_by_user(turn.run_id):
            snapshots._persist_cancelled_partial(turn)

    except Exception as exc:
        nested_pause = hooks.find_hook_paused(exc)
        if nested_pause is not None:
            await pauses._pause_for_hook(turn, nested_pause)
            return
        nested_unknown = errors.find_tool_outcome_unknown(exc)
        if nested_unknown is not None:
            await pauses._pause_for_tool_outcome(turn, nested_unknown)
            return
        logger.error("chat_run_failed", run_id=turn.run_id, error=str(exc), exc_info=True)
        try:
            user_facing = resolve_user_facing_error(exc)
        except Exception:
            user_facing = "请求处理失败，请稍后重试"

        failure_text = str(exc)

        def _commit_failed_output(db) -> None:
            # Whatever the turn produced before failing stays in the row, with
            # the error beside it. Runs in the failed-terminal transaction so a
            # fenced worker can never leave a late message behind.
            model_steps.close_dangling_calls(turn.model_steps)
            services_ChatService_module.ChatService(db).upsert_message(
                chat_id=turn.chat_id,
                role="assistant",
                content=turn.full_response,
                model=turn.model_name,
                thinking=snapshots._thinking_payload(turn),
                tool_calls=turn.tool_calls_log if turn.tool_calls_log else None,
                model_steps=snapshots._model_steps_payload(turn),
                message_id=turn.current_message_id,
                chat_seq=turn.current_chat_seq,
                error={"error": failure_text, "timestamp": clock._utcnow().isoformat()},
                extra_data=snapshots._turn_extra(
                    turn,
                    duration_ms=int((time.monotonic() - turn.run_started_monotonic) * 1000),
                    segments=turn.segments.payload(),
                ),
                commit=False,
            )

        failed = turn.journal.complete(
            turn.run_id,
            owner=turn.owner,
            status="failed",
            failure_reason=str(exc)[:1000],
            last_event_offset=turn.offsets.last,
            commit_effect=_commit_failed_output,
        )
        if not failed:
            logger.warning(
                "chat_run_failure_cas_lost",
                run_id=turn.run_id,
                owner=turn.owner,
            )
            return
        await turn.emit(
            {
                "type": "error",
                "error": user_facing,
                "delta": user_facing,
                "chat_id": turn.chat_id,
            },
            terminal=True,
        )
        await turn.emit({"type": settings.TERMINAL_TYPE, "chat_id": turn.chat_id}, terminal=True)

    finally:
        if checkpoint_task is not None:
            checkpoint_task.cancel()
        # After the terminal state, keep the event stream for the full human
        # wait window plus a recovery grace period so late reconnects can replay.
        await writer.expire_stream(turn.run_id)
        turn.lease_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn.lease_task
        ownership._acknowledge_terminal_writer(turn.run_id)
