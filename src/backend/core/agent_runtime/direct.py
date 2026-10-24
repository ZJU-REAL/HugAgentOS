"""Coordinate the direct agent lifecycle."""

from __future__ import annotations
from core.agent_runtime.integrations import memory
from core.agent_runtime.integrations import memory_session
from core.ontology import output_review_state
from core.ontology import runtime_pack
from core.runs.tool_effects import errors
from core.llm.citations import allocator

import asyncio
import logging
import typing as _typing
from contextlib import aclosing as _aclosing
from typing import Any, AsyncIterator, Dict, List, Optional

import core.agent_runtime.budget as budget
import core.agent_runtime.integrations.evolution as evolution
import core.agent_runtime.integrations.memory_budget as memory_budget
import core.agent_runtime.integrations.ontology_evidence as ontology_evidence
import core.agent_runtime.integrations.ontology_repair as ontology_repair
import core.agent_runtime.targets as targets
import core.chat.message_parser as message_parser
import core.capabilities.catalog.resolver as catalog_resolver


from core.agent_runtime import prepare_direct, stream_direct
from core.agent_runtime.direct_state import DirectTurn

logger = logging.getLogger(__name__)


async def _astream_subagent_direct(
    *,
    agent_id: str,
    session_messages: List[Dict[str, Any]],
    user_message: str,
    context: Dict[str, Any],
) -> AsyncIterator[Dict[str, Any]]:
    """Stream a direct conversation with a user-created sub-agent.

    Loads the UserAgent config from DB and uses it to build the agent
    with custom system_prompt, MCP tools, skills, KB, and model params.
    Shares the same streaming/memory/citation infrastructure as the main route.
    """
    turn = DirectTurn(context=context, session_messages=session_messages)
    from core.llm.api_runtime.agent import apply_api_scope
    from core.harness.registry.evaluation import apply_evaluation_scope

    turn.context = apply_evaluation_scope(apply_api_scope(turn.context))

    import time as _time

    turn.wf_start = _time.monotonic()
    turn.user_agent = targets._load_direct_user_agent(
        agent_id,
        str(turn.context.get("user_id", "")),
        {
            "enabled_skill_ids": catalog_resolver.enabled_skill_ids_from_context(turn.context)
            or [],
            "enabled_mcp_ids": catalog_resolver.enabled_mcp_ids_from_context(turn.context) or [],
            "enabled_kb_ids": catalog_resolver.enabled_kb_ids_from_context(turn.context) or [],
            "sandbox_tools_enabled": bool(turn.context.get("sandbox_tools_enabled", False)),
            "code_capability_enabled": bool(turn.context.get("code_capability_enabled", False)),
        },
    )
    from core.harness import log_writer

    _direct_log_started = _time.monotonic()
    turn.direct_tool_count = 0
    turn.direct_skill_count = 0
    _direct_log_finished = False
    _is_call_subagent_dispatch = turn.context.get("direct_agent_source") == "explicit_command_tool"
    import uuid

    from core.infra.task_lifecycle import cancel_and_join, settle_task

    turn.direct_log_id = uuid.uuid4().hex
    _direct_finish_task = None
    _direct_start_task = asyncio.create_task(
        log_writer.start_subagent_log(
            {
                "id": turn.direct_log_id,
                "user_id": str(turn.context.get("user_id", "")) or None,
                "chat_id": turn.context.get("chat_id"),
                "subagent_name": turn.user_agent.name,
                "subagent_type": (
                    "user_agent" if _is_call_subagent_dispatch else "user_agent_direct"
                ),
                "subagent_id": agent_id,
                "input_messages": {
                    "task": user_message,
                    "invocation": (
                        "call_subagent"
                        if _is_call_subagent_dispatch
                        else turn.context.get("direct_agent_source") or "dedicated_chat"
                    ),
                },
            }
        )
    )

    async def _finish_direct_log(
        status: str,
        *,
        output: str = "",
        error: Optional[str] = None,
        usage: Optional[Dict[str, int]] = None,
    ) -> None:
        nonlocal _direct_log_finished, _direct_finish_task

        async def record():
            await _direct_start_task
            await log_writer.finish_subagent_log(
                turn.direct_log_id,
                status=status,
                output_content=output or None,
                token_usage=usage,
                tool_calls_count=turn.direct_tool_count,
                skill_calls_count=turn.direct_skill_count,
                error_message=error,
                duration_ms=int((_time.monotonic() - _direct_log_started) * 1000),
            )

        if _direct_finish_task is None:
            _direct_finish_task = asyncio.create_task(record())
        await asyncio.shield(_direct_finish_task)
        _direct_log_finished = True

    _direct_exit_status = "cancelled"
    _direct_exit_error = "direct agent execution interrupted"
    try:
        await asyncio.shield(_direct_start_task)
        turn.ontology_runtime = turn.context.get("ontology_runtime")
        if not isinstance(turn.ontology_runtime, dict):
            turn.ontology_runtime = {}
            turn.context["ontology_runtime"] = turn.ontology_runtime
        activations = runtime_pack.activate_runtime_for_asset(
            turn.ontology_runtime,
            kind="subagent",
            asset_id=agent_id,
            tags=list((turn.user_agent.extra_config or {}).get("ontology_tags") or []),
        )
        if activations:
            await asyncio.to_thread(
                ontology_evidence._record_ontology_activations,
                activations,
                turn.ontology_runtime,
                turn.context,
            )

        # ── [memory] Non-blocking retrieval: background task, skipped on budget timeout ───
        turn.memory_user_id = str(turn.context.get("user_id", ""))
        turn.memory_workspace_id = str(turn.context.get("workspace_id", "") or "default")
        _mem0_chat_id = turn.context.get("chat_id") or turn.context.get("conversation_id")
        turn.memory_enabled = bool(turn.context.get("memory_enabled", False))
        _mem0_write_enabled = bool(turn.context.get("memory_write_enabled", False))
        # Under a team project chats.py passes "team:<tid>"; personal/default spaces don't pass it and fall back to the real user_id
        _mem0_scope_user_id = str(
            turn.context.get("memory_scope_user_id", "") or turn.memory_user_id
        )
        logger.info(
            "[subagent] user=%s scope=%s ws=%s agent=%s enabled=%s",
            turn.memory_user_id,
            _mem0_scope_user_id,
            turn.memory_workspace_id,
            agent_id,
            turn.memory_enabled,
        )

        turn.session_memory = await memory_session.open_session_memory(
            chat_id=_mem0_chat_id,
            scope_user_id=_mem0_scope_user_id,
            workspace_id=turn.memory_workspace_id,
            user_message=user_message,
            memory_enabled=turn.memory_enabled,
            # The retrieval budget is part of the orchestration profile: how long
            # this task type is willing to wait for memory is an assembly decision,
            # not a global constant. Resolved here because retrieval starts before
            # the agent is built.
            budget_ms=memory_budget._profile_memory_budget_ms(turn.context),
        )

        warnings: _typing.List[str] = []
        turn.full_response = ""
        turn.displayed_tools: set[str] = set()
        turn.all_citations: _typing.List[_typing.Dict[str, _typing.Any]] = []
        turn.ontology_event_cursor = 0
        turn.ontology_trace: _typing.List[_typing.Dict[str, _typing.Any]] = []
        # 证据锚点发号器：跨轮续号；创建后绑到 agent 上（见下方 attach_allocator），
        # 中间件与本函数由此共享同一个计数器
        turn.anchor_allocator = allocator.AnchorAllocator(
            await asyncio.to_thread(
                allocator.anchor_start_for_chat,
                str(turn.context.get("chat_id") or "") or None,
            )
        )

        try:
            async with _aclosing(prepare_direct.prepare_direct(turn)) as phase:
                async for event in phase:
                    yield event
            async with _aclosing(stream_direct.stream_direct(turn)) as phase:
                async for event in phase:
                    yield event

        except Exception as e:
            import traceback

            logger.error("subagent_stream_error: %s\n%s", e, traceback.format_exc())
            warnings.append(f"Streaming error: {str(e)[:200]}")

            unknown_outcome = errors.find_tool_outcome_unknown(e)
            if unknown_outcome is not None:
                if turn.streaming_agent is not None:
                    await turn.streaming_agent.shutdown()
                await _finish_direct_log("failed", error=str(unknown_outcome)[:200])
                if unknown_outcome is e:
                    raise
                raise unknown_outcome from e

            if not turn.full_response:
                # No answer was produced: the caller gets the failure itself. Tool
                # results already streamed stay visible; the run is recorded as
                # failed with a structured error instead of a fabricated reply that
                # would read as a completed answer.
                if turn.streaming_agent is not None:
                    await turn.streaming_agent.shutdown()
                await _finish_direct_log("failed", error=str(e)[:200])
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
            and output_review_state.claim_output_review(
                turn.ontology_runtime, owner=_ontology_review_owner_id
            )
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
                    repaired, _, cursor, tool_count = (
                        await ontology_repair._run_ontology_repair_round(
                            streaming_agent=turn.streaming_agent,
                            context=turn.context,
                            payload=payload,
                            runtime=turn.ontology_runtime,
                            trace=turn.ontology_trace,
                            citations=turn.all_citations,
                            allocator=turn.anchor_allocator,
                            event_cursor=turn.ontology_event_cursor,
                            subagent_log_id=turn.direct_log_id,
                            event_sink=repair_event_queue.put,
                        )
                    )
                finally:
                    turn.streaming_agent.restore_context_usage(primary_context_usage)
                turn.ontology_event_cursor = cursor
                turn.direct_tool_count += tool_count
                return repaired

            turn.review_task = asyncio.create_task(
                review_ontology_output(
                    task=user_message,
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
                while not turn.review_task.done() or not repair_event_queue.empty():
                    try:
                        repair_event = await asyncio.wait_for(
                            repair_event_queue.get(),
                            timeout=0.1,
                        )
                    except asyncio.TimeoutError:
                        continue
                    yield repair_event
                review = await turn.review_task
            except (asyncio.CancelledError, GeneratorExit):
                await cancel_and_join(turn.review_task)
                output_review_state.release_output_review(
                    turn.ontology_runtime, owner=_ontology_review_owner_id
                )
                await turn.streaming_agent.shutdown()
                raise
            except Exception as exc:  # noqa: BLE001
                if not turn.review_task.done():
                    turn.review_task.cancel()
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

        _direct_usage = await turn.streaming_agent.aget_usage()
        _direct_context_usage = turn.streaming_agent.get_context_usage(_direct_usage)
        await turn.streaming_agent.shutdown()
        await _finish_direct_log(
            "success",
            output=turn.full_response,
            usage=_direct_usage,
        )
        yield {
            "type": "meta",
            "route": f"subagent:{agent_id}",
            "is_markdown": message_parser.looks_markdown(turn.full_response),
            "sources": message_parser.resolve_sources_conflict([]),
            "artifacts": [],
            "warnings": warnings,
            "citations": turn.all_citations,
            "usage": _direct_usage,
            "context_usage": _direct_context_usage,
            # Internal-only handoff consumed by chat_run_executor.  Its whitelist
            # intentionally keeps this prompt/tool surface out of SSE and message
            # metadata while allowing post-turn compaction to use the exact same
            # frozen inputs as pre-turn compaction.
            "_compaction_budget_inputs": budget._compaction_budget_inputs(
                turn.agent, turn.ctx_window
            ),
            "ontology_governance": ontology_evidence._ontology_governance_summary(
                turn.ontology_runtime
            ),
            # Same contract as the main route: memory writes are scheduled below,
            # so the frame can only announce "settling" for the client's skeleton
            # card. Without this marker subagent turns never showed their card.
            "evolution_pending": evolution._evolution_pending_marker(
                turn.context, _mem0_write_enabled
            ),
        }

        # ── [memory] Post-response pipeline (SSE already closed, user isn't waiting) ────
        # No memory is written unless the user opted into memory_write_enabled (first gate).
        if _mem0_write_enabled:
            memory.save_memories_background(
                turn.memory_user_id,
                user_message,
                turn.full_response,
                _mem0_write_enabled,
                workspace_id=turn.memory_workspace_id,
                chat_id=_mem0_chat_id,
                scope_user_id=_mem0_scope_user_id,
                message_id=str(turn.context.get("message_id") or ""),
            )
        else:
            logger.debug(
                "[subagent] memory save skipped: write_enabled=False (user=%s)",
                turn.memory_user_id,
            )

        # ── [evolution] Evidence assembly — same contract as the main route. Without
        # registration the settlement runner parks the memory report on a 45s
        # watchdog before settling, which pushed subagent cards past the client's
        # polling budget.
        if not turn.context.get("agent_api_scope") and not turn.context.get("evaluation_session"):
            evolution._assemble_episode_background(
                message_id=str(turn.context.get("message_id") or ""),
                run_id=str(turn.context.get("run_id") or ""),
                chat_id=str(turn.context.get("chat_id") or ""),
                user_id=str(turn.context.get("user_id") or ""),
                objective=user_message,
                agent=turn.streaming_agent,
                memory_task=turn.session_memory.retrieval_task,
                latency_ms=None,
                memory_write_enabled=_mem0_write_enabled,
            )
    except BaseException as exc:
        if not isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            _direct_exit_status = "failed"
            _direct_exit_error = str(exc)[:200]
        raise
    finally:
        try:
            if turn.review_task is not None:
                await cancel_and_join(turn.review_task)
            if turn.review_owner is not None:
                output_review_state.release_output_review(turn.ontology_runtime, owner=turn.review_owner)
            if turn.direct_stream is not None:
                await turn.direct_stream.aclose()
            if not _direct_log_finished and turn.streaming_agent is not None:
                await turn.streaming_agent.shutdown()
        finally:
            if not _direct_log_finished:
                await settle_task(
                    asyncio.create_task(
                        _finish_direct_log(_direct_exit_status, error=_direct_exit_error)
                    )
                )
