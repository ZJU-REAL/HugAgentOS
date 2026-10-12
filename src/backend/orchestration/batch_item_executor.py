"""Execute one isolated batch item through the normal streaming toolkit."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.llm.context_adapter import append_context_text
from core.llm.context_ir import KIND_REMINDER
from core.llm.factory import create_agent_executor

logger = logging.getLogger(__name__)


async def _fallback_recover_final_text(agent: Any, prompt_preview: str) -> str:
    """Fallback recovery when the main path failed to capture a final reply.

    Two steps:
      Step 1 — Walk memory in reverse, accepting **only assistant messages without tool_use blocks**.
               Those are the genuine text-only terminal replies; this never mistakes a prelude
               like "Now calling tool X:" for the result.
      Step 2 — If Step 1 found nothing, the agent exited abnormally before synthesizing a final
               reply (compaction failure / upstream returned empty / gateway timeout, etc.).
               Inject a system-hint into memory and call ``agent.reply()`` once more to force the
               model to produce a final Chinese answer based on the existing tool_results. If that
               also fails, return an empty string so the caller records ``status="skipped"``
               instead of treating a dirty prelude as success.
    """
    from core.llm.message_compat import extract_text_from_chat_response

    # ── Step 1 ──
    try:
        # AgentScope 2.0: agent.memory removed → agent.state.context: list[Msg]
        mem = list(agent.state.context)
        for msg in reversed(mem or []):
            if getattr(msg, "role", None) != "assistant":
                continue
            has_tu = (
                msg.has_content_blocks("tool_call")  # 2.0: tool_use → tool_call
                if hasattr(msg, "has_content_blocks")
                else False
            )
            if has_tu:
                # Skip — this one is just a reasoning prelude to a tool call, not the terminal reply.
                continue
            text = (extract_text_from_chat_response(msg) or "").strip()
            if text:
                logger.info(
                    "[batch] fallback step1: picked text-only assistant len=%d "
                    "preview=%r prompt=%r",
                    len(text),
                    text[:120],
                    prompt_preview,
                )
                return text
    except Exception as e:
        logger.warning("[batch] fallback step1 memory walk failed: %s", e)

    # ── Step 2 ──
    logger.warning(
        "[batch] fallback step1 found nothing — entering step2: re-prompt " "synthesis. prompt=%r",
        prompt_preview,
    )
    try:
        synthesis_hint = (
            "<system-hint>之前的工具调用已完成，但你尚未给出最终答复。"
            "请基于已获取的工具结果，**直接用中文写出最终答案**，"
            "不要再调用任何工具，不要再输出推理过程或新的工具调用计划，"
            "只输出给用户看的正文。</system-hint>"
        )
        append_context_text(
            agent,
            synthesis_hint,
            kind=KIND_REMINDER,
            origin="harness:batch_synthesis",
            trust="system",
            priority=900,
            token_budget=2_000,
        )
        reply_msg = await agent.reply(inputs=None)
        text = (extract_text_from_chat_response(reply_msg) or "").strip()
        if text:
            logger.info(
                "[batch] fallback step2: synthesis succeeded len=%d preview=%r",
                len(text),
                text[:120],
            )
            return text
        logger.warning("[batch] fallback step2: synthesis returned empty text")
    except Exception as e:
        logger.warning("[batch] fallback step2 synthesis failed: %s", e)

    # Everything failed: return empty so the caller classifies it as skipped/incomplete.
    return ""


async def _run_item_via_workflow(
    prompt: str,
    user_id: str,
    sub_mcp_ids: List[str],
    *,
    chat_id: str,
    run_id: str,
    journal_owner: str,
    sub_skill_ids: Optional[List[str]] = None,
    sub_visible_agents: Optional[List[Dict[str, Any]]] = None,
    project_ctx: Optional[Dict[str, Any]] = None,
) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run a single batch item through the streaming-agent pipeline.

    Captures the same surface the regular chat bubble renders:
      • `text` — concatenated text deltas (final answer)
      • `tool_calls` — per call: name, display_name, args, output, status
      • `artifacts` — file references extracted from tool results
      • `citations` — knowledge-base / search citations

    Returns ``(text, tool_calls, artifacts, citations)``.
    """
    from core.chat.tool_log import attach_tool_result as _attach_tool_result
    from core.chat.tool_log import upsert_tool_call as _upsert_tool_call
    from core.config.display_names import TOOL_DISPLAY_NAMES
    from core.content.artifact_refs import extract_file_refs
    from core.llm import workspace as _workspace_mod
    from core.services.artifact_service import (
        extend_collected_artifacts as _extend_collected_artifacts,
    )
    from orchestration.citation_anchor import (
        AnchorAllocator,
        attach_allocator,
        collect_citation_dicts,
    )
    from orchestration.streaming import StreamingAgent

    tool_calls_log: List[Dict[str, Any]] = []
    artifacts: List[Dict[str, Any]] = []
    citations: List[Dict[str, Any]] = []
    # 证据锚点：每个批量 item 一个独立发号器（item 间互不续号，item 内全局唯一）；
    # 中间件在结果回给模型前注号并登记，这里按 tool_id 精确取回
    _anchor_allocator = AnchorAllocator()
    from core.services.ontology_service import build_user_ontology_runtime

    ontology_enabled, ontology_runtime = build_user_ontology_runtime(
        user_id=user_id,
        task=prompt,
    )

    # ⚠️ isolated=True is mandatory for the batch runner. Two things must both happen:
    #   1) skip the shared MCP pool and spawn brand-new stdio subprocesses per item
    #   2) skip HTTP-transport MCP (streamable_http / sse)
    #
    # Going back to isolated=False and reusing the pool was tried: StreamingAgent has a
    # _wait_reply background task that force-cancels reply_task 10s after the stream ends,
    # and that cancel, exiting through the pool's shared anyio scope, triggers a cross-task
    # bug which cancels the next item's setup as collateral — symptom: item 0 OK, but item 1
    # gets cancelled right at startup. In other words, as long as anyio resources are shared
    # across items, the cancel-scope cross-task landmine remains.
    #
    # Every item must get its own stdio clients (no cross-contamination) + no HTTP calls
    # (when an HTTP client is GC'd, its __aexit__ exiting the anyio scope in the wrong task
    # is the same underlying bug; see history commits).
    agent, clients = await create_agent_executor(
        user_query=prompt,
        enabled_skill_ids=sub_skill_ids,
        enabled_mcp_ids=sub_mcp_ids,
        current_user_id=user_id,
        project_ctx=project_ctx,
        chat_id=chat_id,
        run_id=run_id,
        journal_owner=journal_owner,
        isolated=True,
        max_iters=50,
        visible_subagents=sub_visible_agents if sub_visible_agents else None,
        # The conversation is the only sandbox boundary. Batch items keep their own model,
        # MCP and workspace-UI state, but share this chat's live sandbox filesystem.
        ontology_runtime=ontology_runtime,
    )
    # 证据锚点：发号器绑到 agent，中间件与本函数共享同一计数器
    attach_allocator(agent, _anchor_allocator)
    streaming_agent = StreamingAgent(agent, clients)

    # In a ReAct loop the agent emits multiple rounds of text deltas:
    # round 1 reasoning → tool_call → tool_result → round 2 reasoning →
    # tool_call → ... → final answer. We only want the LAST round
    # (the final answer), so we reset ``current_round_text`` whenever a
    # new tool_call fires — anything still buffered at end of stream is
    # the final answer.
    current_round_text = ""
    prompt_preview = (prompt or "")[:80]
    # Strict workspace gate: each batch item gets its own scoped state so
    # nothing leaks between items or back to the parent caller.
    _ws_ctx = _workspace_mod.scope()
    _ws_ctx.__enter__()
    try:
        # NOTE: do NOT pre-add the prompt to agent.memory here.
        # ``StreamingAgent.stream()`` pops the last user message out of
        # ``session_messages`` and passes it to ``agent.reply(user_msg)``,
        # which adds it to memory internally. Manually adding it first
        # causes the same prompt to appear **twice** in agent memory,
        # which (a) wastes context budget, (b) makes the OpenAI formatter
        # emit two identical user-role messages in a row, confusing the
        # model and (c) breaks any pre_reply hook that walks "the last
        # user message" — it finds the manual copy instead of the one
        # ``reply()`` is about to add. The main chat path in
        # ``workflow.py`` already follows this contract (see comment at
        # workflow.py:263 — "agent.reply() will add it to memory
        # internally, avoiding duplicates").
        async for event_type, payload in streaming_agent.stream(
            session_messages=[{"role": "user", "content": prompt}],
            context={
                "user_id": user_id,
                "chat_id": chat_id,
                "run_id": run_id,
                "journal_owner": journal_owner,
                "model_name": DEFAULT_CHAT_MODEL_ALIAS,
                "enable_thinking": False,
                "ontology_enabled": ontology_enabled,
                "ontology_runtime": ontology_runtime,
            },
        ):
            if event_type == "text_delta":
                current_round_text += payload
            elif event_type == "tool_call":
                # Reasoning-prelude text was a setup for *this* tool call;
                # it isn't the final answer to the user, so drop it.
                current_round_text = ""

                tool_name = payload.get("name", "unknown")
                tool_args = payload.get("args", {})
                tool_id = payload.get("id", "")
                _upsert_tool_call(
                    tool_calls_log,
                    {
                        "tool_name": tool_name,
                        "tool_display_name": TOOL_DISPLAY_NAMES.get(tool_name, tool_name),
                        "tool_args": tool_args if isinstance(tool_args, dict) else {},
                        "tool_id": tool_id,
                    },
                )
            elif event_type == "tool_result":
                tool_name = payload.get("name", "unknown")
                tool_id = payload.get("id", "")
                tool_content = payload.get("content", "")
                try:
                    tool_result_json = json.loads(tool_content) if tool_content else {}
                except (json.JSONDecodeError, TypeError):
                    tool_result_json = {"result": tool_content}
                _attach_tool_result(tool_calls_log, tool_id, tool_name, tool_result_json)
                # Pull file/artifact refs out of the tool result so the UI
                # can render Word/Excel/PPT/chart download cards.
                refs = extract_file_refs(tool_result_json)
                for ref in refs:
                    ref["tool_name"] = tool_name or ""
                _extend_collected_artifacts(artifacts, refs)
                # Citations (KB hits, internet search)：按 tool_id 从发号器注册表精确取
                citations.extend(collect_citation_dicts(tool_id, _anchor_allocator))
            elif event_type == "error":
                if isinstance(payload, BaseException):
                    raise payload
                raise RuntimeError(str(payload))
            # heartbeat / tool_pending / thinking_delta — ignored for batch

        full_response = current_round_text

        # Fallback: main path empty → walk memory in reverse for a text-only assistant
        # message; if none, inject a system-hint and have the model re-synthesize.
        # See _fallback_recover_final_text for details.
        if not full_response.strip():
            full_response = await _fallback_recover_final_text(
                agent=agent,
                prompt_preview=prompt_preview,
            )

        from core.ontology.validator import requires_output_review

        if full_response and requires_output_review(ontology_runtime):
            from orchestration.subagents.ontology_reviewer import review_ontology_output

            trace = [
                {
                    "type": "tool_result",
                    "tool_name": item.get("tool_name"),
                    "tool_id": item.get("tool_id"),
                    "result": item.get("result"),
                }
                for item in tool_calls_log
                if "result" in item
            ]
            review = await review_ontology_output(
                task=prompt,
                answer=full_response,
                runtime=ontology_runtime,
                trace=trace,
                citations=citations,
                user_id=user_id,
                chat_id=None,
                model_name=DEFAULT_CHAT_MODEL_ALIAS,
            )
            full_response = review["answer"]

        # Strict workspace gate: replace artifacts list with the pinned
        # subset BEFORE leaving the scope (state is reset on __exit__).
        artifacts = _workspace_mod.get_pinned()
    finally:
        try:
            _ws_ctx.__exit__(None, None, None)
        except Exception:
            pass
        # Deliberately no cleanup: (streaming_agent, clients) are all stuffed into a
        # process-level resident list so they are never GC'd during the batch. Once GC runs,
        # the AsyncExitStack.__aexit__ of the clients exits the anyio cancel scope from the
        # wrong task, and the cancel signal propagates along the event loop to the next item
        # → that item gets cancelled on the spot. The cost is that subprocesses + fds/sockets
        # linger until the worker process exits — acceptable compared to a hang.
        _persistent_clients.append((streaming_agent, list(clients)))

    return full_response, tool_calls_log, artifacts, citations


# Per-process strong-ref list — every (streaming_agent, clients) tuple
# created during a batch run gets appended here and is never freed. The
# point is to keep Python from GC-ing them mid-batch: GC would call
# AsyncExitStack.__aexit__ on the MCP clients in some background task,
# which exits anyio cancel scopes from the wrong task and that cancel
# signal cascades into whichever item is currently spawning subprocesses.
# Released only when the worker process exits / is restarted.
_persistent_clients: list = []
