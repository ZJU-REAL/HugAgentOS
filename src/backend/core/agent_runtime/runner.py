"""Coordinate the main agent lifecycle."""

from __future__ import annotations
from core.agent_runtime.integrations import memory
from core.agent_runtime.integrations import memory_session
from core.ontology import output_review_state
from core.ontology import runtime_pack
from core.runs.tool_effects import errors
from core.llm.citations import allocator
from core.ontology import runtime_policy

import asyncio
import logging
import typing as _typing
from contextlib import aclosing as _aclosing
from typing import Any, Dict, List

import core.agent_runtime.budget as budget
import core.agent_runtime.direct as direct
import core.agent_runtime.events as events
import core.agent_runtime.integrations.evolution as evolution
import core.agent_runtime.integrations.memory_budget as memory_budget
import core.agent_runtime.integrations.ontology_evidence as ontology_evidence
import core.agent_runtime.integrations.ontology_repair as ontology_repair
import core.agent_runtime.skills as skills
import core.agent_runtime.targets as targets
import core.chat.message_parser as message_parser
import core.chat.plan_progress as plan_progress


from core.agent_runtime import prepare_main, stream_main
from core.agent_runtime.main_state import MainTurn

logger = logging.getLogger(__name__)


async def astream_chat_workflow(
    *, session_messages: List[Dict[str, Any]], user_message: str, context: Dict[str, Any]
):
    """Stream a chat turn and close its resources on every consumer exit."""
    turn = MainTurn(context=context, session_messages=session_messages, user_message=user_message)
    try:
        async with _aclosing(_stream_turn(turn)) as stream:
            async for event in stream:
                yield event
    finally:
        from core.infra.task_lifecycle import cancel_and_join

        if turn.review_task is not None:
            await cancel_and_join(turn.review_task)
        if turn.review_owner is not None:
            output_review_state.release_output_review(turn.ontology_runtime, owner=turn.review_owner)
        await _close_turn(turn)


async def _close_turn(turn: MainTurn) -> None:
    if turn.streaming_agent is not None and not turn.closed:
        turn.closed = True
        await turn.streaming_agent.shutdown()


async def _stream_turn(turn: MainTurn):
    """Stream route -> handoff -> target execution.

    Yields chunks in the format:
    - {"type": "content", "delta": "text chunk"}
    - {"type": "tool_call", ...}
    - {"type": "tool_result", ...}
    - {"type": "meta", "route": "...", "sources": [...], ...}
    """

    from core.llm.api_runtime.agent import apply_api_scope
    from core.harness.registry.evaluation import apply_evaluation_scope

    turn.context = apply_evaluation_scope(apply_api_scope(turn.context))

    # ── Persistent dedicated sub-agent conversation mode ──
    # Per-turn @mentions deliberately remain on the main route so the parent
    # model emits the real call_subagent tool event and keeps normal streaming.
    _agent_id = targets._direct_agent_id_from_context(turn.context)
    if _agent_id:

        async with _aclosing(
            direct._astream_subagent_direct(
                agent_id=_agent_id,
                session_messages=turn.session_messages,
                user_message=turn.user_message,
                context=turn.context,
            )
        ) as direct_stream:
            async for chunk in direct_stream:
                yield chunk
        return

    _request_ontology_runtime = turn.context.get("ontology_runtime")
    if not isinstance(_request_ontology_runtime, dict):
        _request_ontology_runtime = {}
        turn.context["ontology_runtime"] = _request_ontology_runtime
    explicit_skill_ids = skills._explicit_skill_ids_from_context(turn.context)
    for skill_id in explicit_skill_ids:
        await asyncio.to_thread(
            runtime_policy.resolve_runtime_asset_tags,
            runtime=_request_ontology_runtime,
            kind="skill",
            asset_id=skill_id,
            user_id=str(turn.context.get("user_id", "") or ""),
        )
        activations = runtime_pack.activate_runtime_for_asset(
            _request_ontology_runtime,
            kind="skill",
            asset_id=skill_id,
        )
        if activations:
            await asyncio.to_thread(
                ontology_evidence._record_ontology_activations,
                activations,
                _request_ontology_runtime,
                turn.context,
            )

    # ── Inject explicit skill instructions ──
    skill_msg = skills._build_skill_injection(turn.context)
    if skill_msg:
        turn.session_messages.insert(-1, skill_msg)
        logger.info(
            "[skill_inject] injected skill instructions for '%s'",
            turn.context.get("skill_id"),
        )

    # ── [memory] Session memory opened here, never awaited on the SSE path ──
    # First turn of a chat: retrieval starts as a background task so it overlaps
    # agent assembly, and Fact injection is skipped if it blows its budget.
    # Every later turn: the stored snapshot is replayed and no retrieval runs.
    turn.memory_user_id = str(turn.context.get("user_id", ""))
    turn.memory_workspace_id = str(turn.context.get("workspace_id", "") or "default")
    _mem0_chat_id = turn.context.get("chat_id") or turn.context.get("conversation_id")
    turn.memory_enabled = bool(turn.context.get("memory_enabled", False))
    _mem0_write_enabled = bool(turn.context.get("memory_write_enabled", False))
    # Under a team project chats.py passes "team:<tid>"; personal/default spaces don't pass it and fall back to the real user_id
    _mem0_scope_user_id = str(turn.context.get("memory_scope_user_id", "") or turn.memory_user_id)
    logger.info(
        "[memory] user=%s scope=%s ws=%s chat=%s enabled=%s write=%s",
        turn.memory_user_id,
        _mem0_scope_user_id,
        turn.memory_workspace_id,
        _mem0_chat_id,
        turn.memory_enabled,
        _mem0_write_enabled,
    )

    turn.session_memory = await memory_session.open_session_memory(
        chat_id=_mem0_chat_id,
        scope_user_id=_mem0_scope_user_id,
        workspace_id=turn.memory_workspace_id,
        user_message=turn.user_message,
        memory_enabled=turn.memory_enabled,
        # The retrieval budget is part of the orchestration profile: how long
        # this task type is willing to wait for memory is an assembly decision,
        # not a global constant. Resolved here because retrieval starts before
        # the agent is built.
        budget_ms=memory_budget._profile_memory_budget_ms(turn.context),
    )

    # ── Main-route streaming ──────────────────────────────────────
    warnings: _typing.List[str] = []
    turn.full_response = ""
    turn.displayed_tools: set[str] = set()
    turn.all_citations: _typing.List[_typing.Dict[str, _typing.Any]] = []
    turn.ontology_runtime = _request_ontology_runtime
    turn.ontology_event_cursor = 0
    turn.ontology_trace: _typing.List[_typing.Dict[str, _typing.Any]] = []
    turn.last_plan: _typing.Optional[_typing.Dict[str, _typing.Any]] = None
    turn.pending_plan_updates: _typing.Dict[str, _typing.Dict[str, _typing.Any]] = {}
    _stream_errored = False
    # 证据锚点发号器：跨轮续号；创建后绑到 agent 上（见下方 attach_allocator），
    # 中间件与本函数由此共享同一个计数器
    turn.anchor_allocator = allocator.AnchorAllocator(
        await asyncio.to_thread(
            allocator.anchor_start_for_chat, str(turn.context.get("chat_id") or "") or None
        )
    )

    try:
        async with _aclosing(prepare_main.prepare_main(turn)) as phase:
            async for event in phase:
                yield event

        # Project scope is no longer passed via ContextVar — see the comment at
        # the same spot in _astream_subagent_direct above. All scope-dependent
        # tools captured it by closure when registered in agent_factory; the
        # finishing _persist_artifacts gets the scope explicitly from chats.py.

        async with _aclosing(stream_main.stream_main(turn)) as phase:
            async for event in phase:
                yield event

    except Exception as e:
        import traceback

        logger.error("stream_workflow_error: %s\n%s", e, traceback.format_exc())
        warnings.append(f"Streaming error: {str(e)[:200]}")
        _stream_errored = True

        unknown_outcome = errors.find_tool_outcome_unknown(e)
        if unknown_outcome is not None:
            if turn.streaming_agent is not None:
                await _close_turn(turn)
            if unknown_outcome is e:
                raise
            raise unknown_outcome from e

        if not turn.full_response:
            # No answer was produced: propagate. The run executor persists the
            # tool cards already shown together with the error and emits a
            # structured ``error`` event — no fabricated "please refer to the
            # tool results above" reply that would read as a completed answer.
            if turn.streaming_agent is not None:
                await _close_turn(turn)
            raise

    pending_ontology_events = turn.ontology_runtime.get("runtime_events", [])[
        turn.ontology_event_cursor :
    ]
    for ontology_event in pending_ontology_events:
        yield dict(ontology_event)
    turn.ontology_event_cursor += len(pending_ontology_events)

    _ontology_review_owner_id = ontology_evidence._ontology_review_owner(
        turn.ontology_runtime, turn.context
    )
    _ontology_review_claimed = bool(
        turn.full_response
        and output_review_state.claim_output_review(turn.ontology_runtime, owner=_ontology_review_owner_id)
    )
    if _ontology_review_claimed:
        turn.review_owner = _ontology_review_owner_id
        from core.ontology.reviewer import review_ontology_output

        yield {
            "type": "ontology_review",
            "status": "started",
            "level": turn.ontology_runtime.get("review_level", "checkpoint"),
            **ontology_evidence._ontology_review_event_context(turn.ontology_runtime),
        }
        repair_event_queue: asyncio.Queue[_typing.Dict[str, _typing.Any]] = asyncio.Queue()

        async def _remediate(payload: Dict[str, Any]) -> str:
            primary_usage = await turn.streaming_agent.aget_usage()
            primary_context_usage = turn.streaming_agent.get_context_usage(primary_usage)
            try:
                repaired, _, cursor, _ = await ontology_repair._run_ontology_repair_round(
                    streaming_agent=turn.streaming_agent,
                    context=turn.context,
                    payload=payload,
                    runtime=turn.ontology_runtime,
                    trace=turn.ontology_trace,
                    citations=turn.all_citations,
                    allocator=turn.anchor_allocator,
                    event_cursor=turn.ontology_event_cursor,
                    event_sink=repair_event_queue.put,
                )
            finally:
                turn.streaming_agent.restore_context_usage(primary_context_usage)
            turn.ontology_event_cursor = cursor
            return repaired

        turn.review_task = review_task = asyncio.create_task(
            review_ontology_output(
                task=turn.user_message,
                answer=turn.full_response,
                runtime=turn.ontology_runtime,
                trace=turn.ontology_trace,
                citations=turn.all_citations,
                user_id=str(turn.context.get("user_id", "")),
                chat_id=turn.context.get("chat_id"),
                model_name=str(turn.context.get("model_name", "") or "") or None,
                model_provider_id=str(turn.context.get("model_provider_id", "") or "") or None,
                remediate=_remediate,
            )
        )
        try:
            while not review_task.done() or not repair_event_queue.empty():
                try:
                    repair_event = await asyncio.wait_for(
                        repair_event_queue.get(),
                        timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    continue
                yield repair_event
            review = await review_task
        except (asyncio.CancelledError, GeneratorExit):
            from core.infra.task_lifecycle import cancel_and_join

            await cancel_and_join(review_task)
            output_review_state.release_output_review(turn.ontology_runtime, owner=_ontology_review_owner_id)
            await _close_turn(turn)
            raise
        except Exception as exc:  # noqa: BLE001
            if not review_task.done():
                review_task.cancel()
            review = ontology_evidence._ontology_review_failure_result(turn.full_response, exc)
        output_review_state.complete_output_review(
            turn.ontology_runtime,
            owner=_ontology_review_owner_id,
            verdict=str(review.get("verdict") or "unknown"),
            attempts=int(review.get("attempts") or 1),
        )
        turn.ontology_runtime.setdefault("output_review", {}).update(
            {
                "revised": bool(review.get("revised")),
                "annotated": bool(review.get("annotated")),
                "repair_attempts": int(review.get("repair_attempts") or 0),
                "violations": review.get("violations") or [],
                "affected_claims": review.get("affected_claims") or [],
                "evidence": review.get("evidence") or [],
                "feedback": review.get("feedback") or [],
                "manual_review": review.get("manual_review") or {},
                "candidate_answer": (review.get("answer") if review.get("revised") else ""),
                "new_tools": review.get("new_tools") or [],
                "new_citation_count": int(review.get("new_citation_count") or 0),
                "latency_ms": review.get("latency_ms"),
            }
        )
        yield {
            "type": "ontology_review",
            "status": "completed",
            "level": turn.ontology_runtime.get("review_level", "checkpoint"),
            "verdict": review["verdict"],
            "revised": bool(review.get("revised")),
            "annotated": bool(review.get("annotated")),
            "repair_attempts": int(review.get("repair_attempts") or 0),
            "candidate_answer": review.get("answer") if review.get("revised") else "",
            "manual_review": review.get("manual_review") or {},
            "violations": review.get("violations") or [],
            "affected_claims": review.get("affected_claims") or [],
            "evidence": review.get("evidence") or [],
            "feedback": review.get("feedback") or [],
            "new_tools": review.get("new_tools") or [],
            "new_citation_count": int(review.get("new_citation_count") or 0),
            "latency_ms": review.get("latency_ms"),
            **ontology_evidence._ontology_review_event_context(turn.ontology_runtime),
        }

    await _close_turn(turn)

    # update_plan finalization: the model routinely finishes its last step and
    # streams the answer without a closing update_plan call, leaving the plan
    # bar stuck at N-1/N. When the turn ends normally with an answer and no
    # step is still pending, the remaining in_progress step is the one that
    # just produced the answer — close it out.
    if turn.last_plan and turn.full_response and not _stream_errored:
        _plan_steps = turn.last_plan.get("steps") or []
        if (
            _plan_steps
            and all(s.get("status") != "pending" for s in _plan_steps)
            and any(s.get("status") == "in_progress" for s in _plan_steps)
        ):
            _closed = {
                "title": turn.last_plan.get("title", ""),
                "steps": [{**s, "status": "completed"} for s in _plan_steps],
            }
            turn.last_plan = _closed
            plan_progress.save_plan_progress(str(turn.context.get("chat_id") or ""), _closed)
            yield {"type": "plan_update", **_closed}

    # 计划栏收尾 —— 本轮结束时把"还会不会有人来更新这份清单"这件事记进库里。
    #
    # 过去只有前端在流结束时给它盖章，于是收尾依赖"这个标签页恰好在跟这条流"：后台作业
    # 跑完的交付轮是**后端自己发起**的，用户没点任何东西，页面可能压根没跟——计划栏就永远
    # 停在转圈。这里改由服务端认定：本会话还有活着的作业就先不收尾（后面还有交付轮要来
    # 更新它），没有了就记 settled。步骤状态一个字不改，settled 只表示"没有下一轮了"。
    if turn.last_plan:
        _plan_chat_id = str(turn.context.get("chat_id") or "")
        if _plan_chat_id and not events._chat_has_live_job(_plan_chat_id):
            plan_progress.settle_plan_progress(_plan_chat_id)

    _main_usage = await turn.streaming_agent.aget_usage()
    _main_context_usage = turn.streaming_agent.get_context_usage(_main_usage)
    yield {
        "type": "meta",
        "route": "main",
        "is_markdown": message_parser.looks_markdown(turn.full_response),
        "sources": message_parser.resolve_sources_conflict([]),
        "artifacts": [],
        "warnings": warnings,
        "citations": turn.all_citations,
        "usage": _main_usage,
        "context_usage": _main_context_usage,
        "_compaction_budget_inputs": budget._compaction_budget_inputs(turn.agent, turn.ctx_window),
        "ontology_governance": ontology_evidence._ontology_governance_summary(
            turn.ontology_runtime
        ),
        # Settlement has not run yet (memory writes are scheduled below), so all
        # this can honestly say is "settling" — the client draws a skeleton and
        # fills it in when the summary arrives.
        "evolution_pending": evolution._evolution_pending_marker(turn.context, _mem0_write_enabled),
    }

    # ── [memory] Post-response pipeline (SSE already closed, user isn't waiting) ──
    # No memory is written unless the user opted into memory_write_enabled (first gate).
    # Everything goes through memory_pipeline.schedule_post_response_tasks:
    # global Semaphore + 4 extractors + sanitize + write L1/L2/Session + audit,
    # all executed in the background with bounds.
    if _mem0_write_enabled:
        logger.info(
            "[memory] schedule post-response: full_response_len=%s, user=%s ws=%s",
            len(turn.full_response) if turn.full_response else 0,
            turn.memory_user_id,
            turn.memory_workspace_id,
        )
        memory.save_memories_background(
            turn.memory_user_id,
            turn.user_message,
            turn.full_response,
            _mem0_write_enabled,
            workspace_id=turn.memory_workspace_id,
            chat_id=_mem0_chat_id,
            scope_user_id=_mem0_scope_user_id,
            # Lets the pipeline report its real write count back to this turn's
            # settlement instead of the card having to guess one.
            message_id=str(turn.context.get("message_id") or ""),
        )
    else:
        logger.debug(
            "[workflow] memory save skipped: write_enabled=False (user=%s)",
            turn.memory_user_id,
        )

    # ── [evolution] Evidence assembly (SSE already closed, user isn't waiting) ──
    # The response path only bound versions and appended events; joining them
    # into one causal Episode is deliberately deferred to here.
    if not turn.context.get("evaluation_session"):
        evolution._assemble_episode_background(
            message_id=str(turn.context.get("message_id") or ""),
            run_id=str(turn.context.get("run_id") or ""),
            chat_id=str(turn.context.get("chat_id") or ""),
            user_id=str(turn.context.get("user_id") or ""),
            objective=turn.user_message,
            agent=turn.streaming_agent,
            memory_task=turn.session_memory.retrieval_task,
            latency_ms=None,
            memory_write_enabled=_mem0_write_enabled,
        )
