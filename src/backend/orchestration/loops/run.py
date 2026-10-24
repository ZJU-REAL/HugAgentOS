"""Bind autonomous Loop execution to leases and transactional checkpoints."""

from __future__ import annotations
from core.runs.tool_effects import errors as errors_module

from orchestration.loops.presentation import record_loop_event

import asyncio
import time
import typing as _typing
from typing import Any, Dict, List, Optional

import core.db.engine as engine
from core.db.threads import run_db, run_in_session
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.runtime as runtime
import core.runs.settings as settings
import core.chat.service as services_ChatService_module
import core.runs.journal as run_journal
import core.runs.tool_effects.journal as tool_effect_ledger
import orchestration.loops.presentation as _orchestration_loops_presentation
from core.infra.logging import get_logger

logger = get_logger(__name__)


def _project_binding(db, project_id, chat_id):
    """The project context, and whether the loop's chat still needs binding to it."""
    from core.db.models import ChatSession
    from core.projects.edition import build_project_ctx

    project_ctx = build_project_ctx(db, project_id)
    if not project_ctx:
        return project_ctx, False
    sess = db.get(ChatSession, chat_id)
    return project_ctx, sess is not None and sess.project_id != project_id


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
    from orchestration.loops.driver import LoopBudget, run_autonomous_loop
    from orchestration.loops.requirements import GoalSpec

    owner = journal_owner or ownership.new_worker_owner(run_id)
    journal = ownership.journal()
    if not journal.claim(run_id, owner=owner, lease_seconds=settings.RUN_LEASE_SECONDS, pending_only=True):
        logger.info("autonomous_loop_claim_skipped", run_id=run_id, owner=owner)
        return
    current = asyncio.current_task()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        ownership.lease_heartbeat(run_id, owner, current, lease_lost),
        name=f"autonomous_loop_lease:{run_id}",
    )
    # Accumulate the worker's streamed body text + tool cards, and persist them
    # as an assistant message when the stream ends — exactly the same as a
    # normal conversation: the body only accumulates content deltas (thinking
    # goes through separate thinking events, is not stuffed into the body, and
    # no fake </think> is fabricated), and tools are persisted as tool_calls.
    # After a refresh, buildHistorySegments reconstructs identically to a normal
    # conversation.
    content_buf: _typing.List[str] = []
    tool_log: _typing.List[_typing.Dict[str, _typing.Any]] = []
    tool_idx: _typing.Dict[str, int] = {}

    offsets = writer.EventOffsets(journal, run_id, owner, lease_lost=lease_lost)

    async def _emit(event: Dict[str, Any], *, terminal: bool = False) -> None:
        await offsets.take(terminal=terminal)
        et = event.get("type")
        record_loop_event(event, content_buf, tool_log, tool_idx)
        await writer.xadd_event(run_id, offsets.last, event)
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
            services_ChatService_module.ChatService(db).upsert_message(
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
            with engine.SessionLocal() as _db:
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
        except run_journal.RunLeaseLost:
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
    project_ctx: _typing.Optional[_typing.Dict[str, _typing.Any]] = None
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
    from features.automation.loops import LoopService as _LoopSvc

    def _load_ledger() -> Optional[Dict[str, Any]]:
        try:
            with engine.SessionLocal() as db:
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
            found: _typing.List[str] = []

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
        except run_journal.RunLeaseLost:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("loop consume_steering failed", exc_info=True)
            return []

    try:
        if project_id:
            try:
                from core.db.models import ChatSession

                project_ctx, needs_binding = await run_in_session(
                    _project_binding, project_id, chat_id
                )

                if project_ctx and needs_binding:

                    def _commit_project_binding(db) -> None:  # noqa: ANN001
                        sess = db.get(ChatSession, chat_id)
                        if sess is not None:
                            sess.project_id = project_id

                    await run_db(
                        journal.append_operation,
                        run_id,
                        owner=owner,
                        operation_type="loop_project_bound",
                        phase="pre_model",
                        safety="side_effect_committed",
                        payload={"loop_id": loop_id, "project_id": project_id},
                        commit_effect=_commit_project_binding,
                    )
            except run_journal.RunLeaseLost:
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
                # 这几条路径不会被恢复接管，worker 起来的那一刻就是本轮起点，
                # 不必为了拿它再读一次库——首帧前的每一次往返用户都在干等。
                "started_at": int(time.time() * 1000),
                "kind": "autonomous_loop",
                "loop_id": loop_id,
            }
        )

        def _commit_loop_start(db) -> None:  # noqa: ANN001
            from features.automation.loops import LoopService

            if conversational and gs.objective and not is_resume:
                # Only the first start persists the objective; the deterministic
                # id also makes a transaction retry idempotent.
                services_ChatService_module.ChatService(db).add_message(
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
        from core.harness.tool_call_context import CURRENT_RUN_BINDING

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
                is_cancelled=lambda: runtime.is_run_cancelled(run_id),
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

        _asst_content: _typing.Optional[str] = None
        if conversational:
            # Prefer storing the streamed body (matches what the frontend saw); fall back to the trace table when the worker produced no body at all.
            _body = "".join(content_buf).strip()
            _status_zh = _orchestration_loops_presentation._LOOP_STATUS_ZH.get(
                result.status, result.status
            )
            if _body:
                _asst_content = _body + (
                    f"\n\n---\n**{_status_zh}** · 共 {result.iterations} 轮"
                    + (f" · 最终分 {result.final_score}" if result.final_score is not None else "")
                )
            else:
                _asst_content = _orchestration_loops_presentation._loop_transcript_md(
                    gs.objective, result
                )

        def _commit_loop_result(db) -> None:  # noqa: ANN001
            from features.automation.loops import LoopService

            LoopService(db).persist_result(loop_id, result, commit=False)
            if _asst_content is not None:
                # Upsert and the ChatRun terminal transition share one owner-
                # fenced transaction, so a superseded worker cannot publish.
                services_ChatService_module.ChatService(db).upsert_message(
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
            last_event_offset=offsets.last,
            commit_effect=_commit_loop_result,
            committed_operation={
                "operation_type": "loop_result_committed",
                "phase": "loop_result_committed",
                "safety": "side_effect_committed",
                "payload": {"loop_id": loop_id, "message_id": message_id},
            },
        )
        if completed:
            await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
        else:
            logger.warning("autonomous_loop_terminal_cas_lost", run_id=run_id, owner=owner)
    except asyncio.CancelledError:
        # Cancelled: run_autonomous_loop was interrupted and no result is
        # available, so the terminal-state persistence block is skipped — here
        # we persist the progress produced so far (symptoms 2/3); otherwise a
        # reopened chat would only contain the user's objective.
        if lease_lost.is_set():
            logger.warning("autonomous_loop_worker_fenced", run_id=run_id, owner=owner)
            if ownership.cancelled_by_user(run_id):
                _persist_loop_partial()
        else:
            partial_effect = _loop_message_effect("cancelled")
            cancelled = journal.complete(
                run_id,
                owner=owner,
                status="cancelled",
                failure_reason="autonomous loop worker cancelled",
                last_event_offset=offsets.last,
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
                {"type": settings.TERMINAL_TYPE, "chat_id": chat_id, "_cancelled": True},
                terminal=True,
            )
    except Exception as exc:  # noqa: BLE001
        nested_unknown = errors_module.find_tool_outcome_unknown(exc)
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
                await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
            return
        logger.exception("autonomous_loop_run_failed", run_id=run_id)
        partial_effect = _loop_message_effect("failed")
        failed = journal.complete(
            run_id,
            owner=owner,
            status="failed",
            failure_reason=str(exc)[:1000],
            last_event_offset=offsets.last,
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
            await _emit({"type": settings.TERMINAL_TYPE, "chat_id": chat_id}, terminal=True)
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        await writer.expire_stream(run_id)
