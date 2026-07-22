"""Minimal multi-agent workflow orchestration (AgentScope backend)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from core.config.catalog_resolver import (
    enabled_kb_ids_from_context,
    enabled_mcp_ids_from_context,
    enabled_skill_ids_from_context,
)
from core.config.display_names import TOOL_DISPLAY_NAMES
from core.llm.agent_factory import create_agent_executor
from core.llm.context_manager import (
    ContextBudget,
    ContextWindowManager,
    resolve_model_context_window,
)
from core.llm.mcp_manager import close_clients
from core.llm.message_compat import session_to_msgs, strip_thinking
from core.ontology.revision import is_substantive_revision, normalize_revision_candidate
from core.ontology.validator import (
    activate_runtime_for_asset,
    claim_output_review,
    complete_output_review,
    release_output_review,
    requires_output_review,
)
from core.services.ontology_service import resolve_runtime_asset_tags
from core.services.project_scope import edition_project_context_keys
from orchestration.citations import extract_citations_with_offset
from orchestration.streaming import StreamingAgent

# Project mode: extracted from chats.py's ctx and passed through to agent_factory so the system prompt renders the project section.
_PROJECT_CTX_KEYS = (
    "project_id",
    "project_name",
    "project_instructions",
    "project_folder_name",
    "project_folder_kind",
    "project_folder_id",
    "project_files",
) + edition_project_context_keys()


def _extract_project_ctx(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract project-related fields from the workflow context. Returns None when there is no project_id."""
    if not context.get("project_id"):
        return None
    return {k: context.get(k) for k in _PROJECT_CTX_KEYS}


def _extract_skill_id_from_path(path: str) -> str:
    """Extract skill_id from a SKILL.md path (convention: .../skills/<skill_id>/SKILL.md)."""
    if not path:
        return ""
    import os

    norm = path.replace("\\", "/").strip().strip('"').strip("'")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return ""
    if parts[-1].upper() == "SKILL.MD" and len(parts) >= 2:
        return parts[-2]
    base = os.path.basename(norm)
    if base.upper() == "SKILL.MD" and len(parts) >= 2:
        return parts[-2]
    return ""


def _ontology_review_event_context(runtime: Dict[str, Any]) -> Dict[str, Any]:
    committee_size = max(
        (int(pack.get("config", {}).get("committee_size", 3)) for pack in runtime.get("packs", [])),
        default=3,
    )
    workflows = [
        {
            "pack_id": pack.get("pack_id"),
            "workflow_id": workflow.get("id"),
            "workflow_name": workflow.get("name"),
        }
        for pack in runtime.get("packs", [])
        for workflow in pack.get("workflows", [])
    ]
    review_state = runtime.get("output_review") or {}
    return {
        "governance_run_id": runtime.get("governance_run_id"),
        "review_owner": review_state.get("owner"),
        "review_count": int(review_state.get("count") or 0),
        "committee_size": committee_size,
        "workflows": workflows,
        "activation_sources": sorted(
            {
                str(event.get("source"))
                for event in runtime.get("runtime_events", [])
                if event.get("type") == "ontology_activation" and event.get("source")
            }
        ),
    }


def _ontology_governance_summary(runtime: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build the compact, persisted governance card payload for one answer."""
    if not runtime.get("enabled"):
        return None
    events = list(runtime.get("runtime_events") or [])
    activations = [
        {
            key: event.get(key)
            for key in (
                "pack_id",
                "workflow_id",
                "workflow_name",
                "source",
                "asset_kind",
                "asset_id",
                "review_level",
            )
            if event.get(key) is not None
        }
        for event in events
        if event.get("type") == "ontology_activation"
    ]
    gates = [
        {
            key: event.get(key)
            for key in (
                "decision",
                "tool_name",
                "matched_rule_ids",
                "violations",
                "denial_count",
                "circuit_breaker",
            )
            if event.get(key) is not None
        }
        for event in events
        if event.get("type") == "ontology_gate"
    ]
    review_state = dict(runtime.get("output_review") or {})
    review_state["level"] = runtime.get("review_level", "none")
    review_state["committee_size"] = _ontology_review_event_context(runtime)["committee_size"]
    if not activations and not gates and review_state.get("status") == "pending":
        return None
    if review_state.get("status") == "pending" and not requires_output_review(runtime):
        review_state["status"] = "skipped"
    return {
        "governance_run_id": runtime.get("governance_run_id"),
        "activations": activations,
        "gates": gates,
        "review": review_state,
    }


def _ontology_review_owner(runtime: Dict[str, Any], context: Dict[str, Any]) -> str:
    """Stable owner token for the one outer-workflow final review."""
    run_id = runtime.get("governance_run_id") or context.get("chat_id") or "request"
    return f"outer_workflow:{run_id}"


def _ontology_review_failure_result(answer: str, exc: Exception) -> Dict[str, Any]:
    """Keep a completed draft usable when the post-answer review itself fails."""
    logger.error(
        "[ontology-review] automatic review failed; preserving original answer: %s",
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return {
        "verdict": "escalate",
        "answer": answer,
        "revised": False,
        "annotated": True,
        "repair_attempts": 0,
        "attempts": 1,
        "violations": [],
        "affected_claims": [],
        "evidence": [],
        "feedback": ["自动评审未正常完成，已保留原文并转人工复核。"],
        "manual_review": {
            "required": True,
            "title": "领域本体人工复核",
            "summary": "自动评审未正常完成，原文已保留，请人工核对或重新发起评审。",
            "items": [
                {
                    "quote": "本轮完整答案",
                    "rule_id": "ontology_review_runtime",
                    "risk": "自动评审未完成，系统无法确认答案满足全部领域约束。",
                    "manual_check": "核对关键事实、推断、适用前提和原始工具证据。",
                }
            ],
            "actions": ["重新发起本体评审，或在采用结论前完成人工核对。"],
        },
        "new_tools": [],
        "new_citation_count": 0,
        "latency_ms": None,
    }


async def _anext_in_subagent_log_scope(iterator, subagent_log_id: str):  # noqa: ANN001
    """Advance one agent-stream item without leaking log tokens across SSE yields."""
    from core.services import log_service as log_writer

    with log_writer.subagent_scope(subagent_log_id, source="subagent"):
        return await anext(iterator)


def _record_ontology_activations(
    activations: List[Dict[str, Any]],
    runtime: Dict[str, Any],
    context: Dict[str, Any],
) -> None:
    if not activations:
        return
    try:
        from core.services.ontology_service import record_runtime_activation

        for event in activations:
            record_runtime_activation(
                event,
                runtime,
                user_id=str(context.get("user_id", "")) or None,
                chat_id=context.get("chat_id"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ontology] workflow activation audit persistence failed: %s", exc)


def _bounded_trace_value(value: Any, max_chars: int = 4000) -> Any:
    """Keep reviewer evidence useful without copying unbounded tool output."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= max_chars:
        return value
    return {"truncated": True, "preview": text[:max_chars]}


def _parse_tool_result_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value) if value else {}
    except json.JSONDecodeError:
        return {"result": value}


def _capture_nested_ontology_evidence(
    payload: Dict[str, Any],
    trace: List[Dict[str, Any]],
    citations: List[Dict[str, Any]],
    citation_offsets: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Merge trusted ``call_subagent`` bypass events into the outer review trace."""
    sub_type = str(payload.get("sub_type") or "")
    tool_name = str(payload.get("tool_name") or "")
    tool_id = str(payload.get("tool_id") or "")
    provenance = {
        key: payload.get(key)
        for key in ("agent_id", "agent_name", "sub_run_id", "parent_tool_id")
        if payload.get(key) is not None
    }
    if sub_type == "tool_call" and tool_name and payload.get("input") is not None:
        trace.append(
            {
                "type": "tool_call",
                "tool_id": tool_id,
                "tool_name": tool_name,
                "input": _bounded_trace_value(payload.get("input")),
                "source": "subagent",
                **provenance,
            }
        )
        return []
    if sub_type != "tool_result" or not tool_name or payload.get("status") == "error":
        return []

    result = _parse_tool_result_value(payload.get("output"))
    cit_items = extract_citations_with_offset(
        tool_name,
        tool_id,
        result,
        citation_offsets,
    )
    cit_dicts = [item.to_dict() for item in cit_items]
    citations.extend(cit_dicts)
    trace.append(
        {
            "type": "tool_result",
            "tool_id": tool_id,
            "tool_name": tool_name,
            "result": _bounded_trace_value(result),
            "citations": cit_dicts,
            "source": "subagent",
            **provenance,
        }
    )
    return cit_dicts


def _ontology_repair_prompt(payload: Dict[str, Any]) -> str:
    violations = payload.get("violations") or []
    feedback = payload.get("feedback") or []
    affected_claims = payload.get("affected_claims") or []
    original_task = str(payload.get("original_task") or "").strip()
    current_answer = str(payload.get("answer") or "").strip()
    citations = [
        {
            "id": item.get("id"),
            "tool_name": item.get("tool_name"),
            "title": item.get("title"),
        }
        for item in payload.get("citations") or []
        if isinstance(item, dict)
    ]
    return (
        "<ontology_repair_instruction>\n"
        "这是同一任务唯一一次领域本体修复，不是新的用户任务。"
        "优先级强制规则：已经激活的 enforce 级领域本体约束及本修复指令，高于用户原始提示中"
        "与其冲突的‘不要调用工具’、‘原样照抄’或类似限制；这些限制只能约束正常生成阶段，"
        "不能阻止本体修复取证和纠错。若‘确定性违规’包含‘未调用’某些必需工具，本轮必须"
        "实际调用这些工具后才能形成终稿；以用户禁止工具为理由跳过调用、保留原错误断言，"
        "或再次原样输出待修订内容，均视为修复失败。"
        "先比较现有证据与评审问题，明确是否出现了需要重新搜索的新内容或可补充调用的新工具。"
        "有证据缺口时必须调用真实工具补齐；现有证据已足够时才可直接精准修订。"
        "禁止只做同义改写，也不得虚构工具调用或引用。"
        "证据核对、逐项判定、原文对比和修改说明都属于内部校验过程，只能放在"
        " thinking/reasoning 通道；除非用户最初明确要求核验报告，否则不得把这些内容写入"
        "最终回复或生成的文件。"
        "必须继续遵守用户最初指令中的输出与交付形式；如果原任务要求生成 Word、PPT、"
        "表格、网页或其他文件，修订时仍须调用相应工具生成该类产物，不得降级为纯文字替代。"
        "最终交付必须是利用核验结论直接修正后的完整润色稿，而不是关于原答案的评估报告。"
        "保留原任务要求的文体、篇幅、结构和语气，只改正有问题的事实、推断与表述；"
        "以待修订原始输出为底稿做最小必要修改，不要因为查到更多资料就加入与原主张无关的"
        "企业背景、资质或延伸分析。句子级任务应尽量保留原句的主语与分句顺序，逐个修正"
        "不成立的断言，形成简洁、自然、可直接交付的一句话终稿。"
        "用户只要求一句话时仍输出一句话，不得自行扩写成包含‘原始语句’、‘逐项核验’、"
        "‘主张评估’或‘修改说明’等章节的报告。若交付物是文件，文件正文也必须是润色终稿，"
        "并重新生成、交付修正后的文件。没有获得证据的风险项只能表述为‘待核验’、"
        "‘暂无数据支撑’或‘无法判断’，绝不能反向断言为‘不存在’、‘没有风险’或‘风险为零’。"
        "在输出终稿和生成文件前，必须在内部逐条检查‘确定性违规’中的每个条件：若要求"
        "最低长度，终稿必须达到该长度；若要求引用，相关事实后必须包含真实 `[ref:工具名-序号]`"
        "且按规则补齐参考资料；若要求区分事实、推断和待核验项，可在同一句中用分号和简短标签"
        "表达，不得省略。先确定唯一的合格终稿，再把完全相同的正文写入用户要求的文件，"
        "不得先生成文件后又输出一份更短、缺引用或结论不同的候选正文。"
        "思考过程只放在模型的 thinking/reasoning 通道。最终正文必须且只能放在"
        " `<ontology_revision>完整候选答案</ontology_revision>` 中。"
        "在正式输出最终正文前，不要复述、引用或示例化这组标签，也不要用省略号代替正文。"
        "标签内必须是可独立阅读、内容完整的修订答案；若无法修订，则原样放入当前完整答案。"
        "事实、推断和待核验项必须明确区分；引用已有或新增工具结果时使用"
        " `[ref:工具名-序号]`。\n"
        f"用户最初指令：{json.dumps(original_task, ensure_ascii=False)[:12000]}\n"
        f"待修订原始输出：{json.dumps(current_answer, ensure_ascii=False)[:12000]}\n"
        f"修复轮次：{payload.get('attempt', 1)}\n"
        f"问题来源：{payload.get('source', 'review')}\n"
        f"确定性违规：{json.dumps(violations, ensure_ascii=False)[:8000]}\n"
        f"委员会意见：{json.dumps(feedback, ensure_ascii=False)[:8000]}\n"
        f"受影响内容：{json.dumps(affected_claims, ensure_ascii=False)[:5000]}\n"
        f"当前可用引用：{json.dumps(citations, ensure_ascii=False)[:5000]}\n"
        "直接开始补充工具或输出修复后的完整答案。\n"
        "</ontology_repair_instruction>"
    )


async def _run_ontology_repair_round(
    *,
    streaming_agent: StreamingAgent,
    context: Dict[str, Any],
    payload: Dict[str, Any],
    runtime: Dict[str, Any],
    trace: List[Dict[str, Any]],
    citations: List[Dict[str, Any]],
    citation_offsets: Dict[str, int],
    event_cursor: int,
    subagent_log_id: Optional[str] = None,
    event_sink: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Tuple[str, List[Dict[str, Any]], int, int]:
    """Continue the originating agent and stream a separate revision candidate."""
    answer = ""
    events: List[Dict[str, Any]] = []
    tool_count = 0
    text_buffer = ""
    raw_text = ""
    revision_open = False
    revision_closed = False
    thinking_line_prefix = ""

    async def _publish(event: Dict[str, Any]) -> None:
        events.append(event)
        if event_sink is not None:
            await event_sink(event)

    async def _publish_text_thinking(delta: str) -> None:
        """Publish text-channel reasoning and remember its current line prefix."""
        nonlocal thinking_line_prefix
        if not delta:
            return
        if "\n" in delta:
            thinking_line_prefix = delta.rsplit("\n", 1)[-1]
        else:
            thinking_line_prefix = (thinking_line_prefix + delta)[-256:]
        await _publish({"type": "ontology_revision_thinking", "delta": delta})

    def _is_revision_opening_boundary(line_before_open: str) -> bool:
        """Allow a revision wrapper after whitespace or a reasoning close tag."""

        prefix = line_before_open.rstrip()
        return not prefix or prefix.endswith(("</think>", "</analysis>"))

    await _publish(
        {
            "type": "ontology_repair",
            "status": "started",
            "attempt": payload.get("attempt", 1),
            "source": payload.get("source", "review"),
        }
    )

    stream = streaming_agent.stream(
        [{"role": "user", "content": _ontology_repair_prompt(payload)}],
        context,
    ).__aiter__()
    while True:
        try:
            if subagent_log_id:
                event_type, event_payload = await _anext_in_subagent_log_scope(
                    stream,
                    subagent_log_id,
                )
            else:
                event_type, event_payload = await anext(stream)
        except StopAsyncIteration:
            break

        state_runtime = getattr(streaming_agent.agent.state, "ontology_runtime", None)
        if isinstance(state_runtime, dict):
            runtime = state_runtime
            context["ontology_runtime"] = state_runtime
        pending_events = runtime.get("runtime_events", [])[event_cursor:]
        for item in pending_events:
            await _publish(dict(item))
        event_cursor += len(pending_events)

        if event_type == "text_delta":
            delta = str(event_payload or "")
            if not delta:
                continue
            raw_text += delta
            if revision_closed:
                await _publish_text_thinking(delta)
                continue
            text_buffer += delta
            while text_buffer:
                if not revision_open:
                    open_idx = text_buffer.find("<ontology_revision>")
                    if open_idx < 0:
                        keep = min(len("<ontology_revision>") - 1, len(text_buffer))
                        safe_len = len(text_buffer) - keep
                        if safe_len > 0:
                            await _publish_text_thinking(text_buffer[:safe_len])
                            text_buffer = text_buffer[safe_len:]
                        break
                    before_open = text_buffer[:open_idx]
                    line_before_open = (thinking_line_prefix + before_open).rsplit("\n", 1)[-1]
                    if not _is_revision_opening_boundary(line_before_open):
                        # Reasoning models sometimes describe the contract as
                        # `<ontology_revision>...</ontology_revision>`.  A real
                        # wrapper starts a new line or immediately follows the
                        # model's reasoning close tag.  Other inline occurrences
                        # remain folded as examples rather than candidate text.
                        ignored = text_buffer[: open_idx + len("<ontology_revision>")]
                        await _publish_text_thinking(ignored)
                        text_buffer = text_buffer[open_idx + len("<ontology_revision>") :]
                        continue
                    if open_idx > 0:
                        await _publish_text_thinking(before_open)
                    text_buffer = text_buffer[open_idx + len("<ontology_revision>") :]
                    revision_open = True
                    continue
                close_idx = text_buffer.find("</ontology_revision>")
                if close_idx < 0:
                    keep = min(len("</ontology_revision>") - 1, len(text_buffer))
                    safe_len = len(text_buffer) - keep
                    if safe_len > 0:
                        candidate_delta = text_buffer[:safe_len]
                        answer += candidate_delta
                        await _publish({"type": "ontology_revision", "delta": candidate_delta})
                        text_buffer = text_buffer[safe_len:]
                    break
                if close_idx > 0:
                    candidate_delta = text_buffer[:close_idx]
                    answer += candidate_delta
                    await _publish({"type": "ontology_revision", "delta": candidate_delta})
                text_buffer = text_buffer[close_idx + len("</ontology_revision>") :]
                revision_closed = True
                break
        elif event_type == "thinking_delta":
            await _publish(
                {"type": "ontology_revision_thinking", "delta": str(event_payload or "")}
            )
        elif event_type == "tool_call":
            tool_name = str(event_payload.get("name") or "unknown")
            tool_id = str(event_payload.get("id") or "")
            tool_args = event_payload.get("args") or {}
            trace.append(
                {
                    "type": "tool_call",
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "input": _bounded_trace_value(tool_args),
                    "source": "ontology_repair",
                }
            )
            await _publish(
                {
                    "type": "tool_call",
                    "tool_name": tool_name,
                    "tool_display_name": TOOL_DISPLAY_NAMES.get(tool_name, tool_name),
                    "tool_args": tool_args,
                    "input": tool_args,
                    "tool_id": tool_id,
                    "scope": "ontology_revision",
                }
            )
        elif event_type == "tool_result":
            tool_count += 1
            tool_name = str(event_payload.get("name") or "unknown")
            tool_id = str(event_payload.get("id") or "")
            result = _parse_tool_result_value(event_payload.get("content"))
            cit_items = extract_citations_with_offset(
                tool_name,
                tool_id,
                result,
                citation_offsets,
            )
            cit_dicts = [item.to_dict() for item in cit_items]
            citations.extend(cit_dicts)
            trace.append(
                {
                    "type": "tool_result",
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "result": _bounded_trace_value(result),
                    "citations": cit_dicts,
                    "source": "ontology_repair",
                }
            )
            await _publish(
                {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "result": result,
                    "tool_id": tool_id,
                    "citations": cit_dicts,
                    "scope": "ontology_revision",
                }
            )
        elif event_type == "subagent_event":
            nested_citations = _capture_nested_ontology_evidence(
                event_payload or {},
                trace,
                citations,
                citation_offsets,
            )
            sub_type = str((event_payload or {}).get("sub_type") or "")
            if sub_type in {"start", "thinking", "content", "tool_call", "tool_result", "end"}:
                await _publish(
                    {
                        "type": "subagent_event",
                        **(event_payload or {}),
                        "scope": "ontology_revision",
                        **({"citations": nested_citations} if nested_citations else {}),
                    }
                )
        elif event_type == "tool_pending":
            await _publish(
                {
                    "type": "tool_pending",
                    **(event_payload or {}),
                    "scope": "ontology_revision",
                }
            )
        elif event_type in {"file_confirm", "design_pick"}:
            raise RuntimeError("本体自动修复轮不支持等待交互确认，请转人工复核")
        elif event_type == "error":
            if isinstance(event_payload, BaseException):
                raise event_payload
            raise RuntimeError(str(event_payload))

    if revision_open and not revision_closed and text_buffer:
        answer += text_buffer
        await _publish({"type": "ontology_revision", "delta": text_buffer})
    elif not revision_open and text_buffer.strip():
        # Compatibility fallback for models that ignored the wrapper.  When
        # embedded </think> text exists, only the content after the last closing
        # marker can be a candidate; never promote the hidden reasoning itself.
        fallback = normalize_revision_candidate(raw_text.rsplit("</think>", 1)[-1])
        if is_substantive_revision(fallback):
            answer = fallback
            await _publish({"type": "ontology_revision", "delta": answer})

    await _publish(
        {
            "type": "ontology_repair",
            "status": "completed",
            "attempt": payload.get("attempt", 1),
            "tool_calls": tool_count,
        }
    )
    return normalize_revision_candidate(answer), events, event_cursor, tool_count


# SSE tool-result payload builders (moved to routing.tool_payloads)
from orchestration.tool_payloads import (  # noqa: E402
    _FAST_EMIT_TOOLS,
    _build_read_artifact_payload,
    _build_read_tool_payload,
    _build_skill_load_payload,
    _build_view_text_file_payload,
    _tool_args_ready,
)

# Process-level persistent references: after each streaming run,
# (streaming_agent, mcp_clients) is pushed in so HTTP transport MCP clients
# (currently retrieve_dataset_content, a streamable_http client) are never GC'd.
#
# Cause: the HTTP client uses an anyio TaskGroup + CancelScope. For stdio
# clients, streaming_agent.shutdown() SIGTERMs the subprocess directly and
# cleans up fine; HTTP clients (`_process is None`) are skipped by shutdown and
# eventually land in Python GC running __aexit__ — that __aexit__ almost
# certainly runs on the wrong task, triggering
#   RuntimeError: Attempted to exit cancel scope in a different task...
# The cancel signal flows back through the event loop and takes out the current
# stream's agent.reply() / the next item's create_subprocess_exec along with
# it. Reproduced in both production and local.
#
# Same root cause and same fix as batch_orchestrator._persistent_clients:
# accept a small memory leak (one leftover HTTP keepalive socket per
# conversation) in exchange for a runner that never deadlocks. Released
# together when the worker process exits/restarts.
_persistent_clients: list = []


from orchestration.memory_integration import (  # noqa: F401
    build_frozen_memory_block,
    build_user_identity_block,
    inject_frozen_memory,
    launch_memory_retrieval,
    save_memories_background,
)

# Re-export public helpers for backward compatibility
from orchestration.message_parser import looks_markdown as _looks_markdown  # noqa: F401
from orchestration.message_parser import resolve_sources_conflict as _resolve_sources_conflict

logger = logging.getLogger(__name__)


_BATCH_RUNNER_ID = "batch_runner"


def _resolve_batch_runner_visibility(
    context: Dict[str, Any],
    enabled_mcp_ids: Optional[List[str]],
) -> Optional[List[str]]:
    """Decide whether ``batch_runner`` should be in the effective MCP set.

    - ``batch_chat`` (App Center batch-execution entry): force-include, even if the
      user's catalog config doesn't list it. Wins over the unattended hide.
    - ``plan_chat`` / ``automation_run`` / ``disable_batch_plan``: hide,
      because the batch_plan flow requires a confirmation dialog and these
      runs have no UI to confirm with.
    - Otherwise: pass through unchanged.

    When *enabled_mcp_ids* is ``None`` we resolve the catalog default once.
    """
    if context.get("batch_chat"):
        if enabled_mcp_ids is None:
            from core.config.catalog import get_enabled_ids

            enabled_mcp_ids = list(get_enabled_ids("mcp"))
        if _BATCH_RUNNER_ID not in enabled_mcp_ids:
            return [*enabled_mcp_ids, _BATCH_RUNNER_ID]
        return enabled_mcp_ids

    if not (
        context.get("plan_chat")
        or context.get("automation_run")
        or context.get("disable_batch_plan")
    ):
        return enabled_mcp_ids

    if enabled_mcp_ids is None:
        from core.config.catalog import get_enabled_ids

        enabled_mcp_ids = list(get_enabled_ids("mcp"))
    return [m for m in enabled_mcp_ids if m != _BATCH_RUNNER_ID]


def _explicit_skill_ids_from_context(context: Dict[str, Any]) -> List[str]:
    skill_ids: List[str] = []
    single = context.get("skill_id")
    if single:
        skill_ids.append(str(single))
    multi = context.get("skill_ids")
    if isinstance(multi, list):
        skill_ids.extend(str(item) for item in multi if item)
    seen: set[str] = set()
    return [item for item in skill_ids if not (item in seen or seen.add(item))]


def _build_skill_injection(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build an explicit-invocation hint message (skill load instructions + MCP tool activation notice).

    Trigger sources:
    - ``skill_id`` (single skill, slash-command selection) / ``skill_ids``
      (skill list, expanded from an explicitly referenced plugin)
      → inject each skill's SKILL.md load instruction.
    - ``mcp_ids`` (MCP servers activated together when a plugin is explicitly
      referenced) → these tools are already force-enabled into the toolset
      (see _build_ctx); here we append a sentence telling the model they are
      ready and can be called on demand.

    Aligned with Claude Code / Codex: once enabled, MCP tools stay resident in
    the toolset and the model calls them by description on its own — no
    per-tool pinning; explicit invocation only ensures "skill instructions are
    present + plugin tools are activated with a hint to prefer them".

    Returns a dict {"role": "user", "content": "..."} or None.
    """
    skill_ids = _explicit_skill_ids_from_context(context)

    mcp_ids = [str(m) for m in (context.get("mcp_ids") or []) if m]
    plugin_name = context.get("plugin_name")

    if not skill_ids and not mcp_ids:
        return None

    sections: List[str] = []

    # ── Skills: load instructions ──
    if skill_ids:
        try:
            from core.agent_skills.loader import get_skill_loader

            loader = get_skill_loader()
            metadata_all = loader.load_all_metadata()
            entries: List[str] = []
            for sid in skill_ids:
                meta = metadata_all.get(sid)
                if not meta:
                    logger.warning("[skill_inject] skill_id=%s not found", sid)
                    continue
                # Trigger materialization (DB skill written to disk →
                # bind-mounted/pushed into the sandbox), but the injected
                # prompt must use the **sandbox path** /workspace/skills/<id>,
                # not the backend materialized path returned by get_skill_dir
                # (/app/storage/sandbox_skills/<id>) — the backend path does
                # not exist inside the sandbox, and if the model uses it with
                # bash (cat/ls/python) it gets No such file or directory.
                # Same scheme as agent_factory._SKILL_INSTRUCTION_TEMPLATE:
                # the basename is the skill id; on view_text_file reads,
                # skill_tool._resolve_skill_path maps it back to the backend file.
                skill_dir = loader.get_skill_dir(sid)
                if not skill_dir:
                    logger.warning("[skill_inject] skill_id=%s has no skill dir", sid)
                    continue
                sandbox_dir = f"/workspace/skills/{skill_dir.rstrip('/').split('/')[-1]}"
                entries.append(
                    f'- 「{meta.name}」：view_text_file(file_path="{sandbox_dir}/SKILL.md")'
                )
            if entries:
                sections.append(
                    "技能（必须先加载文件再执行，不要跳过直接调用 bash 或其它工具）：\n"
                    + "\n".join(entries)
                )
        except Exception as e:  # noqa: BLE001
            logger.error("[skill_inject] failed to load skills %s: %s", skill_ids, e)

    # ── MCP: activation notice (the tools themselves are already in the toolset; call by description) ──
    if mcp_ids:
        sections.append(
            f"MCP 工具：本插件包含的 {len(mcp_ids)} 个 MCP 工具服务已激活并就绪，"
            "可直接按需调用（无需加载文件），请优先使用它们完成相关任务。"
        )

    if not sections:
        return None

    header = (
        f"用户已显式调用插件「{plugin_name}」，请优先采用其能力："
        if plugin_name
        else "用户已显式指定使用以下能力，请优先采用："
    )
    return {
        "role": "user",
        "content": (
            "<explicit_invocation>\n"
            + header
            + "\n"
            + "\n\n".join(sections)
            + "\n</explicit_invocation>"
        ),
    }


def _parse_agent_mentions(message: str, available_agents: list) -> list:
    """Parse @agent_name mentions from user message.

    Returns list of matched ``agent_id``s in the order they appear in
    *message*.

    When several agent names share a prefix (e.g. ``搜索`` vs ``搜索助手``)
    a naive ``"@搜索" in message`` check matches "@搜索" *inside*
    "@搜索助手", so typing ``@搜索助手`` would falsely also mention the
    shorter ``搜索`` agent — and the prompt hint built downstream would
    instruct the LLM to call both. We sort candidates by name length
    descending and reserve consumed character ranges so the longest
    matching name wins and prefix-shadow matches are skipped.
    """
    if not available_agents:
        return []

    candidates = [a for a in available_agents if a.get("name")]
    if not candidates:
        return []
    candidates.sort(key=lambda a: len(a["name"]), reverse=True)

    consumed: list = []  # non-overlapping (start, end) char ranges

    def _overlaps(start: int, end: int) -> bool:
        for s, e in consumed:
            if start < e and end > s:
                return True
        return False

    # (position, agent_id) — sorted at the end to preserve message order
    hits: list = []
    for agent in candidates:
        token = "@" + agent["name"]
        start = 0
        while True:
            idx = message.find(token, start)
            if idx < 0:
                break
            end = idx + len(token)
            if not _overlaps(idx, end):
                consumed.append((idx, end))
                hits.append((idx, agent["agent_id"]))
            start = idx + len(token)

    hits.sort(key=lambda x: x[0])
    seen: set = set()
    ordered: list = []
    for _, aid in hits:
        if aid not in seen:
            seen.add(aid)
            ordered.append(aid)
    return ordered


def _direct_agent_id_from_context(context: Dict[str, Any]) -> Optional[str]:
    """Return the target only for a persistent dedicated sub-agent chat.

    A per-turn @mention must stay on the main-model stream so it produces a
    genuine ``call_subagent`` tool call instead of silently bypassing the
    parent model's reasoning and streaming lifecycle.
    """
    value = context.get("agent_id")
    return str(value) if value else None


def _load_direct_user_agent(agent_id: str, user_id: str):
    """Load and detach the complete user-agent configuration for execution."""
    from core.db.engine import SessionLocal
    from core.services.user_agent_service import UserAgentService

    with SessionLocal() as db:
        user_agent = UserAgentService(db).get_raw_by_id(agent_id, user_id=user_id)
        # Eagerly load every relationship/config field consumed after the DB
        # session closes. SQLAlchemy expires lazy relationships on close.
        _ = user_agent.mcp_server_ids, user_agent.skill_ids, user_agent.kb_ids
        _ = user_agent.system_prompt, user_agent.model_provider_id
        _ = user_agent.max_iters, user_agent.temperature, user_agent.max_tokens
        _ = user_agent.timeout, user_agent.extra_config
        return user_agent


# ------------------------------------------------------------------
# Data containers
# ------------------------------------------------------------------


@dataclass
class WorkflowResult:
    route: str = "main"
    response: str = ""
    is_markdown: bool = False
    sources: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------
# Synchronous workflow (non-streaming)
# ------------------------------------------------------------------


def run_chat_workflow(
    *,
    session_messages: List[Dict[str, Any]],
    user_message: str,
    context: Dict[str, Any],
) -> WorkflowResult:
    """Run route -> target execution."""

    _explicit_command = context.get("explicit_subagent_command")
    _explicit_agent_id = (
        str(_explicit_command.get("agent_id"))
        if isinstance(_explicit_command, dict) and _explicit_command.get("agent_id")
        else None
    )
    _mention_agent_id = str(context.get("mention_agent_id") or "") or None
    _direct_agent_id = _direct_agent_id_from_context(context)
    _direct_user_agent = None
    _visible_subagents: List[Dict[str, Any]] = []
    _request_ontology_runtime = context.get("ontology_runtime")
    if not isinstance(_request_ontology_runtime, dict):
        _request_ontology_runtime = {}
        context["ontology_runtime"] = _request_ontology_runtime
    if _direct_agent_id:
        _direct_user_agent = _load_direct_user_agent(
            _direct_agent_id,
            str(context.get("user_id", "")),
        )
        activations = activate_runtime_for_asset(
            _request_ontology_runtime,
            kind="subagent",
            asset_id=_direct_agent_id,
            tags=list((_direct_user_agent.extra_config or {}).get("ontology_tags") or []),
        )
        _record_ontology_activations(
            activations,
            _request_ontology_runtime,
            context,
        )
    else:
        try:
            from core.db.engine import SessionLocal as _SessionLocal
            from core.services.user_agent_service import UserAgentService as _UAS

            with _SessionLocal() as _db:
                _visible_subagents = _UAS(_db).list_for_user(str(context.get("user_id", "")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] failed to load visible subagents: %s", exc)
    for skill_id in _explicit_skill_ids_from_context(context):
        resolve_runtime_asset_tags(
            runtime=_request_ontology_runtime,
            kind="skill",
            asset_id=skill_id,
            user_id=str(context.get("user_id", "") or ""),
        )
        activations = activate_runtime_for_asset(
            _request_ontology_runtime,
            kind="skill",
            asset_id=skill_id,
        )
        _record_ontology_activations(
            activations,
            _request_ontology_runtime,
            context,
        )

    # ── Inject explicit skill instructions ──
    skill_msg = _build_skill_injection(context)
    if skill_msg:
        session_messages.insert(-1, skill_msg)
        logger.info("[skill_inject] injected skill instructions for '%s'", context.get("skill_id"))

    warnings: List[str] = []
    enabled_skill_ids = None if _direct_user_agent else enabled_skill_ids_from_context(context)
    enabled_kb_ids = None if _direct_user_agent else enabled_kb_ids_from_context(context)
    enabled_mcp_ids = None if _direct_user_agent else enabled_mcp_ids_from_context(context)

    enabled_mcp_ids = _resolve_batch_runner_visibility(context, enabled_mcp_ids)

    _workflow_user_id = str(context.get("user_id", ""))
    _workflow_model_name = str(context.get("model_name", ""))
    _workflow_model_provider_id = str(context.get("model_provider_id", "") or "")
    _workflow_chat_mode = str(context.get("chat_mode", "") or "")
    _workflow_mem_enabled = bool(context.get("memory_enabled", False))
    _reranker_enabled = bool(context.get("reranker_enabled", False))

    _workflow_batch_chat = bool(context.get("batch_chat", False))

    async def _run():
        agent, mcp_clients = await create_agent_executor(
            agent_spec=None,
            enabled_skill_ids=enabled_skill_ids,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_kb_ids=enabled_kb_ids,
            current_user_id=_workflow_user_id,
            reranker_enabled=_reranker_enabled,
            model_name=_workflow_model_name,
            model_provider_id=_workflow_model_provider_id,
            chat_mode=_workflow_chat_mode,
            memory_enabled=_workflow_mem_enabled,
            batch_mode=_workflow_batch_chat if _direct_user_agent is None else False,
            user_agent=_direct_user_agent,
            visible_subagents=_visible_subagents if _visible_subagents else None,
            chat_id=context.get("chat_id"),
            project_ctx=_extract_project_ctx(context),
            channel_origin=context.get("channel_origin"),
            automation_run=bool(context.get("automation_run")),
            ontology_runtime=context.get("ontology_runtime"),
        )
        try:
            from agentscope.message import Msg, TextBlock

            # AgentScope 2.0: ctx → agent.state (AgentRuntimeState), replacing _jx_context.
            # Uploaded/history files are written into state by
            # apply_request_context, then injected uniformly at reply time by
            # FileContextMiddleware (not hand-copied here).
            try:
                agent.state.apply_request_context(context, user_message or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[workflow] set agent.state failed: %s", exc)

            # PreTurn compaction safety net (symmetric with the streaming path
            # — both workflow entry points protect themselves, and future new
            # callers get it automatically). Zero overhead below the threshold.
            _pt_messages = session_messages
            try:
                from core.services.compaction_service import maybe_run_pre_turn_compaction

                _actual_model = getattr(agent.model, "model_name", _workflow_model_name)
                _pt_messages, _ = await maybe_run_pre_turn_compaction(
                    context.get("chat_id"), session_messages, model_name=_actual_model
                )
            except Exception as _pt_exc:  # noqa: BLE001
                logger.warning("[workflow] pre-turn compaction failed: %s", _pt_exc)

            # Load history EXCLUDING the last user message — reply() adds it.
            history = list(_pt_messages)
            if history and history[-1].get("role") in ("user", "human"):
                history.pop()

            _ctx_mgr = ContextWindowManager.for_model(_workflow_model_name)
            history = _ctx_mgr.trim_history(history)

            if history:
                agent.state.context.extend(session_to_msgs(history))

            # Uploaded-file context is injected uniformly at reply time by
            # FileContextMiddleware (apply_request_context already wrote
            # uploaded_files into state; the middleware validates attachment
            # ownership by state.user_id); no manual append here, otherwise it
            # would duplicate the middleware's injection and waste tokens.

            model_user_message = user_message or ""
            _delegated_agent_id = _explicit_agent_id or _mention_agent_id
            if _delegated_agent_id and _visible_subagents:
                from core.llm.subagent_tool import build_explicit_subagent_command_hint

                explicit_hint = build_explicit_subagent_command_hint(
                    _visible_subagents,
                    _delegated_agent_id,
                )
                if explicit_hint:
                    model_user_message = f"{explicit_hint}\n{model_user_message}"
            user_msg = Msg(
                name="user",
                role="user",
                content=[TextBlock(type="text", text=model_user_message)],
            )
            result = await agent.reply(inputs=user_msg)
            response = strip_thinking(result.get_text_content() or "")
            ontology_runtime = context.get("ontology_runtime")
            if not isinstance(ontology_runtime, dict):
                ontology_runtime = {}
                context["ontology_runtime"] = ontology_runtime
            review_owner = _ontology_review_owner(ontology_runtime, context)
            review_claimed = bool(
                response and claim_output_review(ontology_runtime, owner=review_owner)
            )
            if review_claimed:
                from orchestration.subagents.ontology_reviewer import review_ontology_output

                async def _remediate(payload: Dict[str, Any]) -> str:
                    repair_msg = Msg(
                        name="user",
                        role="user",
                        content=[
                            TextBlock(
                                type="text",
                                text=_ontology_repair_prompt(payload),
                            )
                        ],
                    )
                    repaired_result = await agent.reply(inputs=repair_msg)
                    return strip_thinking(repaired_result.get_text_content() or "").strip()

                try:
                    review = await review_ontology_output(
                        task=user_message,
                        answer=response,
                        runtime=ontology_runtime,
                        trace=[],
                        citations=[],
                        user_id=_workflow_user_id,
                        chat_id=context.get("chat_id"),
                        model_name=_workflow_model_name or None,
                        model_provider_id=_workflow_model_provider_id or None,
                        trace_complete=False,
                        remediate=_remediate,
                    )
                except BaseException:
                    release_output_review(ontology_runtime, owner=review_owner)
                    raise
                complete_output_review(
                    ontology_runtime,
                    owner=review_owner,
                    verdict=str(review.get("verdict") or "unknown"),
                    attempts=int(review.get("attempts") or 1),
                )
                ontology_runtime.setdefault("output_review", {}).update(
                    {
                        "revised": bool(review.get("revised")),
                        "annotated": bool(review.get("annotated")),
                        "repair_attempts": int(review.get("repair_attempts") or 0),
                        "violations": review.get("violations") or [],
                        "affected_claims": review.get("affected_claims") or [],
                        "latency_ms": review.get("latency_ms"),
                    }
                )
                response = review["answer"]
            return response
        finally:
            await close_clients(mcp_clients)

    try:
        import asyncio as _asyncio

        response = _asyncio.run(_run())
    except Exception as e:
        warnings.append(f"Agent execution error: {str(e)[:200]}")
        response = ""

    return WorkflowResult(
        route=f"subagent:{_direct_agent_id}" if _direct_agent_id else "main",
        response=response,
        is_markdown=_looks_markdown(response),
        sources=_resolve_sources_conflict([]),
        artifacts=[],
        warnings=warnings,
        meta={
            "ontology_governance": _ontology_governance_summary(
                context.get("ontology_runtime")
                if isinstance(context.get("ontology_runtime"), dict)
                else {}
            )
        },
    )


# ------------------------------------------------------------------
# Sub-agent direct conversation
# ------------------------------------------------------------------


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
    import time as _time

    _wf_start = _time.monotonic()

    user_agent = _load_direct_user_agent(
        agent_id,
        str(context.get("user_id", "")),
    )
    from core.services import log_service as log_writer

    _direct_log_started = _time.monotonic()
    _direct_tool_count = 0
    _direct_skill_count = 0
    _direct_log_finished = False
    _is_call_subagent_dispatch = context.get("direct_agent_source") == "explicit_command_tool"
    _direct_log_id = await log_writer.start_subagent_log(
        {
            "user_id": str(context.get("user_id", "")) or None,
            "chat_id": context.get("chat_id"),
            "subagent_name": user_agent.name,
            "subagent_type": "user_agent" if _is_call_subagent_dispatch else "user_agent_direct",
            "subagent_id": agent_id,
            "input_messages": {
                "task": user_message,
                "invocation": (
                    "call_subagent"
                    if _is_call_subagent_dispatch
                    else context.get("direct_agent_source") or "dedicated_chat"
                ),
            },
        }
    )

    async def _finish_direct_log(
        status: str,
        *,
        output: str = "",
        error: Optional[str] = None,
        usage: Optional[Dict[str, int]] = None,
    ) -> None:
        nonlocal _direct_log_finished
        if _direct_log_finished:
            return
        _direct_log_finished = True
        await log_writer.finish_subagent_log(
            _direct_log_id,
            status=status,
            output_content=output or None,
            token_usage=usage,
            tool_calls_count=_direct_tool_count,
            skill_calls_count=_direct_skill_count,
            error_message=error,
            duration_ms=int((_time.monotonic() - _direct_log_started) * 1000),
        )

    _ontology_runtime = context.get("ontology_runtime")
    if not isinstance(_ontology_runtime, dict):
        _ontology_runtime = {}
        context["ontology_runtime"] = _ontology_runtime
    activations = activate_runtime_for_asset(
        _ontology_runtime,
        kind="subagent",
        asset_id=agent_id,
        tags=list((user_agent.extra_config or {}).get("ontology_tags") or []),
    )
    if activations:
        await asyncio.to_thread(
            _record_ontology_activations,
            activations,
            _ontology_runtime,
            context,
        )

    # ── [memory] Non-blocking retrieval: background task, skipped on budget timeout ───
    _mem0_user_id = str(context.get("user_id", ""))
    _mem0_workspace_id = str(context.get("workspace_id", "") or "default")
    _mem0_chat_id = context.get("chat_id") or context.get("conversation_id")
    _mem0_enabled = bool(context.get("memory_enabled", False))
    _mem0_write_enabled = bool(context.get("memory_write_enabled", False))
    # Under a team project chats.py passes "team:<tid>"; personal/default spaces don't pass it and fall back to the real user_id
    _mem0_scope_user_id = str(context.get("memory_scope_user_id", "") or _mem0_user_id)
    logger.info(
        "[subagent] user=%s scope=%s ws=%s agent=%s enabled=%s",
        _mem0_user_id,
        _mem0_scope_user_id,
        _mem0_workspace_id,
        agent_id,
        _mem0_enabled,
    )

    _memory_task = await launch_memory_retrieval(
        _mem0_scope_user_id,
        user_message,
        _mem0_enabled,
        workspace_id=_mem0_workspace_id,
    )

    warnings: List[str] = []
    full_response = ""
    displayed_tools: set[str] = set()
    all_citations: List[Dict[str, Any]] = []
    citation_offsets: Dict[str, int] = {}
    _ontology_event_cursor = 0
    _ontology_trace: List[Dict[str, Any]] = []

    try:
        yield {"type": "thinking", "message": "正在连接子智能体..."}
        for ontology_event in _ontology_runtime.get("runtime_events", []):
            yield dict(ontology_event)
        _ontology_event_cursor = len(_ontology_runtime.get("runtime_events", []))

        _stream_user_id = str(context.get("user_id", ""))
        _stream_model_name = str(context.get("model_name", ""))
        _stream_model_provider_id = str(context.get("model_provider_id", "") or "")
        _stream_chat_mode = str(context.get("chat_mode", "") or "")
        _stream_reranker = bool(context.get("reranker_enabled", False))

        # Create agent with sub-agent config overrides
        agent, mcp_clients = await create_agent_executor(
            agent_spec=None,
            enabled_mcp_ids=None,  # overridden by user_agent inside factory
            enabled_skill_ids=None,  # overridden by user_agent inside factory
            enabled_kb_ids=None,  # overridden by user_agent inside factory
            current_user_id=_stream_user_id,
            reranker_enabled=_stream_reranker,
            model_name=_stream_model_name,
            model_provider_id=_stream_model_provider_id,
            chat_mode=_stream_chat_mode,
            memory_enabled=_mem0_enabled,
            user_agent=user_agent,
            # Same as the main agent: pass chat_id → the sandbox session uses
            # the chat_id-keyed "user-bound persistent sandbox" (mounting the
            # per-user credential volumes for lark/dws/email etc.). Omitting it
            # falls back to an ephemeral light sandbox (no credentials) — the
            # root cause of Feishu/DingTalk CLIs reporting "not configured" in
            # direct sub-agent conversations.
            chat_id=context.get("chat_id"),
            project_ctx=_extract_project_ctx(context),
            channel_origin=context.get("channel_origin"),
            automation_run=bool(context.get("automation_run")),
            ontology_runtime=context.get("ontology_runtime"),
        )

        logger.info("[subagent] agent created in %.0fms", (_time.monotonic() - _wf_start) * 1000)

        # ── Frozen-block injection: user identity (always injected) + memory snapshot (loaded only when persistent memory is on) ───
        _identity_block = await build_user_identity_block(_mem0_user_id)
        frozen_block = ""
        if _mem0_enabled:
            frozen_block = await build_frozen_memory_block(
                _mem0_scope_user_id,
                _mem0_workspace_id,
                _memory_task,
                memory_enabled=_mem0_enabled,
            )
        else:
            logger.debug(
                "[subagent] memory load skipped: memory_enabled=False (user=%s)", _mem0_user_id
            )
        if frozen_block or _identity_block:
            session_messages = await inject_frozen_memory(
                frozen_block,
                session_messages,
                identity_block=_identity_block,
            )

        # ── Context window management ─────────────────────────────
        # A sub-agent's context is shared from the main agent (it has no
        # checkpoint system of its own); over budget it is trimmed directly to
        # the token budget (layer-C compression of oversized user messages
        # still happens inside manage_context).
        _actual_model = getattr(agent.model, "model_name", _stream_model_name)
        ctx_manager = ContextWindowManager.for_model(_actual_model)
        trimmed, dropped_messages = ctx_manager.manage_context(session_messages)
        if dropped_messages:
            logger.warning(
                "[subagent] context over budget: dropped %d message(s)", len(dropped_messages)
            )
        session_messages = trimmed

        streaming_agent = StreamingAgent(agent, mcp_clients)
        skill_load_ids: set = set()
        # tool_id → skill_id (parsed from the tool_call's file_path; looked up at tool_result time to replace the SSE payload with the curated detail, avoiding sending the full SKILL.md text to the frontend)
        skill_id_by_tool_id: Dict[str, str] = {}
        # tool_id → tool_args, used at the tool_result stage to recover view_text_file's file_path/ranges
        view_text_file_args: Dict[str, Dict[str, Any]] = {}

        # Project scope is no longer passed via ContextVar — it now travels as
        # explicit parameters along the call chain (agent_factory closes
        # ProjectScope into every register_* tool; chats.py's finishing
        # _persist_artifacts reconstructs the scope from the workflow context
        # and passes it explicitly). See the header comment in
        # core/services/project_scope.py.

        # Never keep a ContextVar token alive across an async-generator
        # ``yield``. The consumer may resume this generator in a copied
        # context, making reset(token) fail with "created in a different
        # Context". Enter the audit scope only while advancing the underlying
        # agent stream; tool execution and persistence happen during anext().
        _direct_stream = streaming_agent.stream(session_messages, context).__aiter__()
        try:
            while True:
                try:
                    event_type, payload = await _anext_in_subagent_log_scope(
                        _direct_stream,
                        _direct_log_id,
                    )
                except StopAsyncIteration:
                    break
                state_runtime = getattr(streaming_agent.agent.state, "ontology_runtime", None)
                if isinstance(state_runtime, dict):
                    _ontology_runtime = state_runtime
                    context["ontology_runtime"] = state_runtime
                pending_ontology_events = _ontology_runtime.get("runtime_events", [])[
                    _ontology_event_cursor:
                ]
                for ontology_event in pending_ontology_events:
                    yield dict(ontology_event)
                _ontology_event_cursor += len(pending_ontology_events)
                if event_type == "text_delta":
                    full_response += payload
                    yield {"type": "content", "event": "ai_message", "delta": payload}

                elif event_type == "thinking_delta":
                    yield {"type": "thinking", "delta": payload}

                elif event_type == "tool_call":
                    tool_name = payload.get("name", "unknown")
                    tool_id = payload.get("id", "")
                    tool_args = payload.get("args", {})

                    _is_fast_emit = tool_name in _FAST_EMIT_TOOLS
                    if tool_id and tool_id in displayed_tools:
                        if _is_fast_emit and tool_args:
                            pass  # re-emit with updated args
                        else:
                            continue
                    if not _tool_args_ready(tool_name, tool_args):
                        continue
                    if tool_id:
                        displayed_tools.add(tool_id)

                    is_skill_load = (
                        tool_name == "view_text_file"
                        and isinstance(tool_args, dict)
                        and "SKILL.md" in str(tool_args.get("file_path", ""))
                    )
                    if is_skill_load and tool_id:
                        skill_load_ids.add(tool_id)
                        _sid = _extract_skill_id_from_path(str(tool_args.get("file_path", "")))
                        if _sid:
                            skill_id_by_tool_id[tool_id] = _sid
                    # Non-skill view_text_file also needs trimming at the tool_result stage → record the args
                    if (
                        tool_name in ("view_text_file", "Read")
                        and not is_skill_load
                        and tool_id
                        and isinstance(tool_args, dict)
                    ):
                        view_text_file_args[tool_id] = tool_args
                    emit_name = "load_skill" if is_skill_load else tool_name
                    display_name = (
                        "加载技能"
                        if is_skill_load
                        else TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                    )
                    safe_args = tool_args if isinstance(tool_args, dict) else {}
                    _ontology_trace.append(
                        {
                            "type": "tool_call",
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "input": _bounded_trace_value(safe_args),
                        }
                    )

                    yield {
                        "type": "tool_call",
                        "tool_name": emit_name,
                        "tool_display_name": display_name,
                        "tool_args": safe_args,
                        "input": safe_args,
                        "tool_id": tool_id,
                    }

                elif event_type == "tool_result":
                    tool_name = payload.get("name", "unknown")
                    tool_id = payload.get("id", "")
                    is_skill_result = (tool_id and tool_id in skill_load_ids) or (
                        tool_name == "view_text_file"
                        and "SKILL.md" in str(payload.get("content", ""))
                    )
                    if is_skill_result:
                        _direct_skill_count += 1
                        tool_name = "load_skill"
                    else:
                        _direct_tool_count += 1
                    tool_content = payload.get("content", "")

                    try:
                        tool_result_json = json.loads(tool_content) if tool_content else {}
                    except json.JSONDecodeError:
                        tool_result_json = {"result": tool_content}

                    # Skill load: replace the full SKILL.md text with the same
                    # curated detail used by the capability center; affects
                    # only the SSE payload sent to the frontend — the agent's
                    # own memory still holds the full content
                    if is_skill_result:
                        _sid = skill_id_by_tool_id.get(tool_id, "") or _extract_skill_id_from_path(
                            str(tool_content)
                        )
                        tool_result_json = _build_skill_load_payload(_sid)
                    elif tool_name == "view_text_file":
                        # Plain file read (AgentScope built-in view_text_file): replace with file metadata + short preview
                        tool_result_json = _build_view_text_file_payload(
                            view_text_file_args.get(tool_id, {}), tool_content
                        )
                    elif tool_name == "Read":
                        # Claude-Code-style Read tool: JSON payload, content holds the whole file
                        tool_result_json = _build_read_tool_payload(
                            view_text_file_args.get(tool_id, {}), tool_result_json
                        )
                    elif tool_name == "read_artifact":
                        tool_result_json = _build_read_artifact_payload(tool_result_json)

                    extracted_query = ""
                    if isinstance(tool_result_json, dict) and "result" in tool_result_json:
                        result_data = tool_result_json["result"]
                        if isinstance(result_data, dict):
                            extracted_query = result_data.get(
                                "query", result_data.get("question", "")
                            )

                    cit_items = extract_citations_with_offset(
                        tool_name, tool_id, tool_result_json, citation_offsets
                    )
                    cit_dicts = [c.to_dict() for c in cit_items]
                    all_citations.extend(cit_dicts)
                    _ontology_trace.append(
                        {
                            "type": "tool_result",
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "result": _bounded_trace_value(tool_result_json),
                            "citations": cit_dicts,
                        }
                    )

                    yield {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "tool_args": {"query": extracted_query} if extracted_query else {},
                        "result": tool_result_json,
                        "tool_id": tool_id,
                        "citations": cit_dicts,
                    }

                elif event_type == "heartbeat":
                    yield {"type": "heartbeat"}

                elif event_type == "tool_pending":
                    yield {"type": "tool_pending", **(payload or {})}

                elif event_type == "subagent_event":
                    # Bypass channel for the sub-agent's internal
                    # thinking/tool_call/tool_result/content — attached under
                    # the call_subagent tool card that launched it (linked via
                    # parent_tool_id).
                    nested_citations = _capture_nested_ontology_evidence(
                        payload or {},
                        _ontology_trace,
                        all_citations,
                        citation_offsets,
                    )
                    yield {
                        "type": "subagent_event",
                        **(payload or {}),
                        **({"citations": nested_citations} if nested_citations else {}),
                    }

                elif event_type in ("file_confirm", "design_pick"):
                    # Confirmation-type events (§13 MySpace write confirm /
                    # site-design pick-one-of-three): a tool coroutine has
                    # suspended waiting for the user's out-of-band action.
                    # Pass through to the frontend to show the confirmation
                    # card; the agent task stays blocked in that tool and this
                    # SSE stream does not end — after the out-of-band
                    # POST /file-confirm the tool resumes in place.
                    yield {"type": event_type, **(payload or {})}

                elif event_type == "error":
                    # payload may be a real exception object (kind=="err") or a
                    # dict (e.g. ExceedMaxIters mapped to {"kind":..,"name":..}).
                    # Raising the latter directly gives a TypeError, masking
                    # the real situation — wrap it in a RuntimeError.
                    if isinstance(payload, BaseException):
                        raise payload
                    raise RuntimeError(str(payload))

        except BaseException:
            # Keep clients alive after a normal first draft so the same agent
            # can continue its ReAct context during ontology repair.
            raise

    except Exception as e:
        import traceback

        logger.error("subagent_stream_error: %s\n%s", e, traceback.format_exc())
        warnings.append(f"Streaming error: {str(e)[:200]}")

        if displayed_tools and not full_response:
            fallback_msg = (
                "抱歉，我在整理工具调用的结果时遇到了问题。以上是已获取的工具执行结果，请参考。"
            )
            full_response = fallback_msg
            yield {"type": "content", "event": "ai_message", "delta": fallback_msg}
        elif not full_response:
            if "streaming_agent" in locals():
                await streaming_agent.shutdown()
                _persistent_clients.append((streaming_agent, list(mcp_clients)))
            await _finish_direct_log("failed", error=str(e)[:200])
            raise

    pending_ontology_events = _ontology_runtime.get("runtime_events", [])[_ontology_event_cursor:]
    for ontology_event in pending_ontology_events:
        yield dict(ontology_event)
    _ontology_event_cursor += len(pending_ontology_events)

    _ontology_review_owner_id = _ontology_review_owner(_ontology_runtime, context)
    _ontology_review_claimed = bool(
        full_response and claim_output_review(_ontology_runtime, owner=_ontology_review_owner_id)
    )
    if _ontology_review_claimed:
        from orchestration.subagents.ontology_reviewer import review_ontology_output

        yield {
            "type": "ontology_review",
            "status": "started",
            "level": _ontology_runtime.get("review_level", "checkpoint"),
            **_ontology_review_event_context(_ontology_runtime),
        }
        repair_event_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        async def _remediate(payload: Dict[str, Any]) -> str:
            nonlocal _ontology_event_cursor, _direct_tool_count
            repaired, _, cursor, tool_count = await _run_ontology_repair_round(
                streaming_agent=streaming_agent,
                context=context,
                payload=payload,
                runtime=_ontology_runtime,
                trace=_ontology_trace,
                citations=all_citations,
                citation_offsets=citation_offsets,
                event_cursor=_ontology_event_cursor,
                subagent_log_id=_direct_log_id,
                event_sink=repair_event_queue.put,
            )
            _ontology_event_cursor = cursor
            _direct_tool_count += tool_count
            return repaired

        review_task = asyncio.create_task(
            review_ontology_output(
                task=user_message,
                answer=full_response,
                runtime=_ontology_runtime,
                trace=_ontology_trace,
                citations=all_citations,
                user_id=str(context.get("user_id", "")),
                chat_id=context.get("chat_id"),
                model_name=str(context.get("model_name", "") or "") or None,
                model_provider_id=str(context.get("model_provider_id", "") or "") or None,
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
        except asyncio.CancelledError:
            if not review_task.done():
                review_task.cancel()
            release_output_review(_ontology_runtime, owner=_ontology_review_owner_id)
            await streaming_agent.shutdown()
            _persistent_clients.append((streaming_agent, list(mcp_clients)))
            raise
        except Exception as exc:  # noqa: BLE001
            if not review_task.done():
                review_task.cancel()
            review = _ontology_review_failure_result(full_response, exc)
        complete_output_review(
            _ontology_runtime,
            owner=_ontology_review_owner_id,
            verdict=str(review.get("verdict") or "unknown"),
            attempts=int(review.get("attempts") or 1),
        )
        _ontology_runtime.setdefault("output_review", {}).update(
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
            "level": _ontology_runtime.get("review_level", "checkpoint"),
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
            **_ontology_review_event_context(_ontology_runtime),
        }

    _direct_usage = streaming_agent.get_usage()
    await streaming_agent.shutdown()
    _persistent_clients.append((streaming_agent, list(mcp_clients)))
    await _finish_direct_log(
        "success",
        output=full_response,
        usage=_direct_usage,
    )
    yield {
        "type": "meta",
        "route": f"subagent:{agent_id}",
        "is_markdown": _looks_markdown(full_response),
        "sources": _resolve_sources_conflict([]),
        "artifacts": [],
        "warnings": warnings,
        "citations": all_citations,
        "usage": _direct_usage,
        "ontology_governance": _ontology_governance_summary(_ontology_runtime),
    }

    # ── [memory] Post-response pipeline (SSE already closed, user isn't waiting) ────
    # No memory is written unless the user opted into memory_write_enabled (first gate).
    if _mem0_write_enabled:
        save_memories_background(
            _mem0_user_id,
            user_message,
            full_response,
            _mem0_write_enabled,
            workspace_id=_mem0_workspace_id,
            chat_id=_mem0_chat_id,
            scope_user_id=_mem0_scope_user_id,
        )
    else:
        logger.debug("[subagent] memory save skipped: write_enabled=False (user=%s)", _mem0_user_id)


# ------------------------------------------------------------------
# Streaming workflow
# ------------------------------------------------------------------


async def astream_chat_workflow(
    *,
    session_messages: List[Dict[str, Any]],
    user_message: str,
    context: Dict[str, Any],
):
    """Stream route -> handoff -> target execution.

    Yields chunks in the format:
    - {"type": "content", "delta": "text chunk"}
    - {"type": "tool_call", ...}
    - {"type": "tool_result", ...}
    - {"type": "meta", "route": "...", "sources": [...], ...}
    """

    # ── Persistent dedicated sub-agent conversation mode ──
    # Per-turn @mentions deliberately remain on the main route so the parent
    # model emits the real call_subagent tool event and keeps normal streaming.
    _agent_id = _direct_agent_id_from_context(context)
    if _agent_id:
        async for chunk in _astream_subagent_direct(
            agent_id=_agent_id,
            session_messages=session_messages,
            user_message=user_message,
            context=context,
        ):
            yield chunk
        return

    _request_ontology_runtime = context.get("ontology_runtime")
    if not isinstance(_request_ontology_runtime, dict):
        _request_ontology_runtime = {}
        context["ontology_runtime"] = _request_ontology_runtime
    explicit_skill_ids = _explicit_skill_ids_from_context(context)
    for skill_id in explicit_skill_ids:
        await asyncio.to_thread(
            resolve_runtime_asset_tags,
            runtime=_request_ontology_runtime,
            kind="skill",
            asset_id=skill_id,
            user_id=str(context.get("user_id", "") or ""),
        )
        activations = activate_runtime_for_asset(
            _request_ontology_runtime,
            kind="skill",
            asset_id=skill_id,
        )
        if activations:
            await asyncio.to_thread(
                _record_ontology_activations,
                activations,
                _request_ontology_runtime,
                context,
            )

    # ── Inject explicit skill instructions ──
    skill_msg = _build_skill_injection(context)
    if skill_msg:
        session_messages.insert(-1, skill_msg)
        logger.info("[skill_inject] injected skill instructions for '%s'", context.get("skill_id"))

    # ── [memory] Retrieval launched as background task, NOT awaited here ──
    # New non-blocking path: launch_memory_retrieval() returns a Task
    # immediately; the actual result gets a short wait via
    # asyncio.wait_for(timeout=0.05) inside build_frozen_memory_block(); if the
    # budget is exceeded, Fact injection is skipped and only the L1 Profile is
    # used. Never blocks the SSE first frame.
    _mem0_user_id = str(context.get("user_id", ""))
    _mem0_workspace_id = str(context.get("workspace_id", "") or "default")
    _mem0_chat_id = context.get("chat_id") or context.get("conversation_id")
    _mem0_enabled = bool(context.get("memory_enabled", False))
    _mem0_write_enabled = bool(context.get("memory_write_enabled", False))
    # Under a team project chats.py passes "team:<tid>"; personal/default spaces don't pass it and fall back to the real user_id
    _mem0_scope_user_id = str(context.get("memory_scope_user_id", "") or _mem0_user_id)
    logger.info(
        "[memory] user=%s scope=%s ws=%s chat=%s enabled=%s write=%s",
        _mem0_user_id,
        _mem0_scope_user_id,
        _mem0_workspace_id,
        _mem0_chat_id,
        _mem0_enabled,
        _mem0_write_enabled,
    )

    _memory_task = await launch_memory_retrieval(
        _mem0_scope_user_id,
        user_message,
        _mem0_enabled,
        workspace_id=_mem0_workspace_id,
    )

    # ── Main-route streaming ──────────────────────────────────────
    warnings: List[str] = []
    full_response = ""
    displayed_tools: set[str] = set()
    all_citations: List[Dict[str, Any]] = []
    citation_offsets: Dict[str, int] = {}
    _ontology_runtime = _request_ontology_runtime
    _ontology_event_cursor = 0
    _ontology_trace: List[Dict[str, Any]] = []

    try:
        import time as _time

        _wf_start = _time.monotonic()

        yield {"type": "thinking", "message": "正在分析您的问题..."}
        for ontology_event in _ontology_runtime.get("runtime_events", []):
            yield dict(ontology_event)
        _ontology_event_cursor = len(_ontology_runtime.get("runtime_events", []))

        _stream_user_id = str(context.get("user_id", ""))
        _stream_model_name = str(context.get("model_name", ""))
        _stream_reranker = bool(context.get("reranker_enabled", False))
        enabled_skill_ids = enabled_skill_ids_from_context(context)
        enabled_kb_ids = enabled_kb_ids_from_context(context)
        enabled_mcp_ids = enabled_mcp_ids_from_context(context)

        _stream_unattended = bool(
            context.get("plan_chat")
            or context.get("automation_run")
            or context.get("disable_batch_plan")
        )
        enabled_mcp_ids = _resolve_batch_runner_visibility(context, enabled_mcp_ids)

        # ── Load visible sub-agents for main agent routing ──
        _visible_subagents: list = []
        _mentioned_ids: list = []
        _explicit_command = context.get("explicit_subagent_command")
        _explicit_agent_id = (
            str(_explicit_command.get("agent_id"))
            if isinstance(_explicit_command, dict) and _explicit_command.get("agent_id")
            else ""
        )
        _mention_agent_id = str(context.get("mention_agent_id") or "")
        try:
            from core.db.engine import SessionLocal as _SessionLocal
            from core.services.user_agent_service import UserAgentService as _UAS

            with _SessionLocal() as _db:
                _ua_svc = _UAS(_db)
                _visible_subagents = _ua_svc.list_for_user(_stream_user_id)
            # Prefer the structured per-turn target. Text parsing remains a
            # compatibility fallback for callers that only include @name.
            if _visible_subagents:
                _delegated_agent_id = _explicit_agent_id or _mention_agent_id
                if _delegated_agent_id and any(
                    str(item.get("agent_id") or "") == _delegated_agent_id
                    for item in _visible_subagents
                ):
                    _mentioned_ids = [_delegated_agent_id]
                else:
                    _mentioned_ids = _parse_agent_mentions(user_message, _visible_subagents)
        except Exception as _exc:
            logger.warning("[workflow] failed to load visible subagents: %s", _exc)

        # Create agent (with model-aware CompressionConfig + optional native LTM)
        _plan_chat = bool(context.get("plan_chat", False))
        _batch_chat = bool(context.get("batch_chat", False))
        agent, mcp_clients = await create_agent_executor(
            agent_spec=None,
            enabled_skill_ids=enabled_skill_ids,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_kb_ids=enabled_kb_ids,
            current_user_id=_stream_user_id,
            reranker_enabled=_stream_reranker,
            model_name=_stream_model_name,
            memory_enabled=_mem0_enabled,
            visible_subagents=_visible_subagents if _visible_subagents else None,
            plan_mode=_plan_chat,
            batch_mode=_batch_chat,
            chat_id=context.get("chat_id"),
            project_ctx=_extract_project_ctx(context),
            channel_origin=context.get("channel_origin"),
            automation_run=bool(context.get("automation_run")),
            ontology_runtime=context.get("ontology_runtime"),
            # The single positive source of truth for enter_plan_mode: enabled
            # only for "interactive main chats that can host plan mode" — has
            # a chat_id, not a channel bot, not automation, not plan_chat, not
            # batch. Channels/automation have no UI for the user to confirm a
            # plan; plan_chat/batch have their own orchestration and must not
            # nest plan mode.
            top_level_chat=(
                bool(context.get("chat_id"))
                and not context.get("channel_origin")
                and not bool(context.get("automation_run"))
                and not _plan_chat
                and not _batch_chat
            ),
        )

        logger.info("[workflow] agent created in %.0fms", (_time.monotonic() - _wf_start) * 1000)

        # ── Inject the per-turn sub-agent constraint into the current user message ──
        # Keeps it OUT of the system prompt so the LLM provider's prefix cache
        # hits across turns within a chat (otherwise every turn with different
        # targets would re-build the cache from scratch). A strict natural-
        # language command remains on this normal model stream; it only
        # constrains the model's next real tool call instead of fabricating one
        # in the workflow layer.
        if _mentioned_ids and _visible_subagents:
            from core.llm.subagent_tool import (
                build_explicit_subagent_command_hint,
                build_subagent_mention_hint,
            )

            if _explicit_agent_id or _mention_agent_id:
                _mention_hint = build_explicit_subagent_command_hint(
                    _visible_subagents,
                    _explicit_agent_id or _mention_agent_id,
                )
            else:
                _mention_hint = build_subagent_mention_hint(
                    _visible_subagents,
                    _mentioned_ids,
                )
            if (
                _mention_hint
                and session_messages
                and session_messages[-1].get("role") in ("user", "human")
            ):
                session_messages[-1] = {
                    **session_messages[-1],
                    "content": _mention_hint + "\n" + (session_messages[-1].get("content") or ""),
                }

        # ── PreTurn compaction safety net (aligned with Codex pre-turn compaction) ──
        # When end-of-turn background compaction failed/was skipped, or the
        # previous turn's tool calls blew up the history, compact once
        # synchronously with the same cross-turn compaction mechanism before
        # this turn starts and write a checkpoint. Below the threshold, only a
        # pure byte estimate is done (no DB / no LLM), so first-token latency
        # is unaffected. Must run **before** the frozen memory block injection
        # — identity/memory blocks are re-injected every turn and must not be
        # baked into the persistent checkpoint.
        # The model window is preferentially read straight off the model object
        # (make_chat_model bakes in the real context_size per the Config model
        # configuration at construction time, no default fallback), saving the
        # streaming path one synchronous DB query; only if missing on the
        # object do we fall back to resolve — unconfigured raises, fail loud,
        # never silently run with the wrong window.
        # preturn and manage_context below share the same value.
        _actual_model = getattr(agent.model, "model_name", _stream_model_name)
        _ctx_window = int(getattr(agent.model, "context_size", 0) or 0)
        if _ctx_window <= 0:
            _ctx_window = resolve_model_context_window(_actual_model)
        try:
            from core.services.compaction_service import maybe_run_pre_turn_compaction

            session_messages, _ = await maybe_run_pre_turn_compaction(
                context.get("chat_id"),
                session_messages,
                model_name=_actual_model,
                context_window=_ctx_window,
            )
        except Exception as _pt_exc:  # noqa: BLE001
            logger.warning("[workflow] pre-turn compaction failed: %s", _pt_exc)

        # ── Frozen-block injection: user identity (always injected) + memory snapshot (loaded only when persistent memory is on) ──
        # The L1 Profile always reads the DB (fast); L2 Facts are injected only
        # if memory_task has already completed — otherwise this turn's Facts
        # are dropped to protect first-frame latency.
        _identity_block = await build_user_identity_block(_mem0_user_id)
        frozen_block = ""
        if _mem0_enabled:
            frozen_block = await build_frozen_memory_block(
                _mem0_scope_user_id,
                _mem0_workspace_id,
                _memory_task,
                memory_enabled=_mem0_enabled,
            )
        else:
            logger.debug(
                "[workflow] memory load skipped: memory_enabled=False (user=%s)", _mem0_user_id
            )
        if frozen_block or _identity_block:
            session_messages = await inject_frozen_memory(
                frozen_block,
                session_messages,
                identity_block=_identity_block,
            )

        # ── Context window management (last line of defense) ────────────
        # PreTurn compaction already acted as the safety net before frozen
        # block injection; here we keep only manage_context's layer C
        # (compressing an oversized single user message) + token-budget
        # trimming. Normally the drop branch is never reached — if it is, we
        # only log (no in-place summarization anymore; summarization belongs
        # entirely to the compaction mechanism).
        ctx_manager = ContextWindowManager(ContextBudget(model_context_window=_ctx_window))
        trimmed, dropped_messages = ctx_manager.manage_context(session_messages)
        if dropped_messages:
            logger.warning(
                "[workflow] context over budget after pre-turn compaction: dropped %d message(s)",
                len(dropped_messages),
            )
        session_messages = trimmed

        streaming_agent = StreamingAgent(agent, mcp_clients)

        skill_load_ids: set = set()  # track tool_ids that are skill loads
        # tool_id → skill_id; looked up at tool_result time to replace the SSE payload with the curated detail
        skill_id_by_tool_id: Dict[str, str] = {}
        # tool_id → tool_args, used at the tool_result stage to recover view_text_file's file_path/ranges
        view_text_file_args: Dict[str, Dict[str, Any]] = {}
        # enter_plan_mode tool arguments (stashed at the tool_call stage; at
        # the tool_result stage they drive the plan_redirect event + aborting
        # this turn) — isomorphic to batch_plan's human-in-the-loop gate.
        enter_plan_args: Dict[str, Any] = {}

        # Project scope is no longer passed via ContextVar — see the comment at
        # the same spot in _astream_subagent_direct above. All scope-dependent
        # tools captured it by closure when registered in agent_factory; the
        # finishing _persist_artifacts gets the scope explicitly from chats.py.

        try:
            async for event_type, payload in streaming_agent.stream(session_messages, context):
                state_runtime = getattr(streaming_agent.agent.state, "ontology_runtime", None)
                if isinstance(state_runtime, dict):
                    _ontology_runtime = state_runtime
                    context["ontology_runtime"] = state_runtime
                pending_ontology_events = _ontology_runtime.get("runtime_events", [])[
                    _ontology_event_cursor:
                ]
                for ontology_event in pending_ontology_events:
                    yield dict(ontology_event)
                _ontology_event_cursor += len(pending_ontology_events)
                if event_type == "text_delta":
                    full_response += payload
                    yield {"type": "content", "event": "ai_message", "delta": payload}

                elif event_type == "thinking_delta":
                    yield {"type": "thinking", "delta": payload}

                elif event_type == "tool_call":
                    tool_name = payload.get("name", "unknown")
                    tool_id = payload.get("id", "")
                    tool_args = payload.get("args", {})

                    # In streaming mode, the first chunk for a tool_call may
                    # arrive with empty args.  For view_text_file we need args
                    # to decide if this is a skill load, so skip empty-arg
                    # duplicates until we get the complete args.
                    _is_fast_emit = tool_name in _FAST_EMIT_TOOLS
                    if tool_id and tool_id in displayed_tools:
                        # Fast-emit tools: re-emit when args arrive so the
                        # frontend can update input display immediately.
                        if _is_fast_emit and tool_args:
                            pass  # fall through to emit update
                        else:
                            continue
                    if not _tool_args_ready(tool_name, tool_args):
                        continue
                    if tool_id:
                        displayed_tools.add(tool_id)

                    # Detect skill loading: view_text_file reading a SKILL.md
                    is_skill_load = (
                        tool_name == "view_text_file"
                        and isinstance(tool_args, dict)
                        and "SKILL.md" in str(tool_args.get("file_path", ""))
                    )
                    if is_skill_load and tool_id:
                        skill_load_ids.add(tool_id)
                        _sid = _extract_skill_id_from_path(str(tool_args.get("file_path", "")))
                        if _sid:
                            skill_id_by_tool_id[tool_id] = _sid
                    if (
                        tool_name in ("view_text_file", "Read")
                        and not is_skill_load
                        and tool_id
                        and isinstance(tool_args, dict)
                    ):
                        view_text_file_args[tool_id] = tool_args
                    emit_name = "load_skill" if is_skill_load else tool_name
                    display_name = (
                        "加载技能"
                        if is_skill_load
                        else TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                    )
                    safe_args = tool_args if isinstance(tool_args, dict) else {}
                    _ontology_trace.append(
                        {
                            "type": "tool_call",
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "input": _bounded_trace_value(safe_args),
                        }
                    )

                    # enter_plan_mode: stash the arguments (in streaming, args
                    # may arrive across frames — take the one carrying
                    # task_description); the tool_result stage uses them to
                    # emit plan_redirect.
                    if tool_name == "enter_plan_mode" and safe_args.get("task_description"):
                        enter_plan_args = safe_args

                    # Resolve sub-agent name for call_subagent tool card
                    _tc_sa_name = ""
                    if tool_name == "call_subagent" and _visible_subagents:
                        _tc_sa_id = safe_args.get("agent_id", "") if safe_args else ""
                        if _tc_sa_id:
                            for _sa in _visible_subagents:
                                if _sa.get("agent_id") == _tc_sa_id:
                                    _tc_sa_name = _sa.get("name", "")
                                    break
                        if not _tc_sa_name and _mentioned_ids:
                            for _sa in _visible_subagents:
                                if _sa.get("agent_id") in _mentioned_ids:
                                    _tc_sa_name = _sa.get("name", "")
                                    break

                    yield {
                        "type": "tool_call",
                        "tool_name": emit_name,
                        "tool_display_name": display_name,
                        "tool_args": safe_args,
                        "input": safe_args,
                        "tool_id": tool_id,
                        **({"subagent_name": _tc_sa_name} if _tc_sa_name else {}),
                    }

                elif event_type == "tool_result":
                    tool_name = payload.get("name", "unknown")
                    tool_id = payload.get("id", "")
                    # Also override tool_name for skill load results
                    is_skill_result = (tool_id and tool_id in skill_load_ids) or (
                        tool_name == "view_text_file"
                        and "SKILL.md" in str(payload.get("content", ""))
                    )
                    if is_skill_result:
                        tool_name = "load_skill"
                    tool_content = payload.get("content", "")

                    # Parse tool result
                    try:
                        tool_result_json = json.loads(tool_content) if tool_content else {}
                    except json.JSONDecodeError:
                        tool_result_json = {"result": tool_content}

                    # Skill load: replace the full SKILL.md text with the same curated detail used by the capability center
                    if is_skill_result:
                        _sid = skill_id_by_tool_id.get(tool_id, "") or _extract_skill_id_from_path(
                            str(tool_content)
                        )
                        tool_result_json = _build_skill_load_payload(_sid)
                    elif tool_name == "view_text_file":
                        # Plain file read (AgentScope built-in view_text_file): replace with file metadata + short preview
                        tool_result_json = _build_view_text_file_payload(
                            view_text_file_args.get(tool_id, {}), tool_content
                        )
                    elif tool_name == "Read":
                        # Claude-Code-style Read tool: JSON payload, content holds the whole file
                        tool_result_json = _build_read_tool_payload(
                            view_text_file_args.get(tool_id, {}), tool_result_json
                        )
                    elif tool_name == "read_artifact":
                        tool_result_json = _build_read_artifact_payload(tool_result_json)

                    # Extract query if present
                    extracted_query = ""
                    if isinstance(tool_result_json, dict) and "result" in tool_result_json:
                        result_data = tool_result_json["result"]
                        if isinstance(result_data, dict):
                            extracted_query = result_data.get(
                                "query", result_data.get("question", "")
                            )

                    # Citations
                    cit_items = extract_citations_with_offset(
                        tool_name, tool_id, tool_result_json, citation_offsets
                    )
                    cit_dicts = [c.to_dict() for c in cit_items]
                    all_citations.extend(cit_dicts)
                    _ontology_trace.append(
                        {
                            "type": "tool_result",
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "result": _bounded_trace_value(tool_result_json),
                            "citations": cit_dicts,
                        }
                    )

                    # Resolve sub-agent name from call_subagent result text
                    _tr_sa_name = ""
                    if tool_name == "call_subagent":
                        _res_str = (
                            str(tool_result_json.get("result", ""))
                            if isinstance(tool_result_json, dict)
                            else str(tool_result_json)
                        )
                        if "【" in _res_str and "】" in _res_str:
                            _tr_sa_name = _res_str.split("【", 1)[1].split("】", 1)[0]

                    yield {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "tool_args": {"query": extracted_query} if extracted_query else {},
                        "result": tool_result_json,
                        "tool_id": tool_id,
                        "citations": cit_dicts,
                        **({"subagent_name": _tr_sa_name} if _tr_sa_name else {}),
                    }

                    # ── enter_plan_mode: switch into plan mode (human-in-the-loop gate, same as batch_plan) ──
                    # The main agent decides the task is complex → calls
                    # enter_plan_mode. Here we emit a plan_redirect event
                    # (carrying task_description) and **abort this turn**: the
                    # frontend uses it to drive the existing plan-mode pipeline
                    # (generate plan → preview card → user confirmation →
                    # execute). The agent does not continue executing on its
                    # own, consistent with the safety semantics of "user
                    # approval required before generating a plan".
                    # The source of truth for tool availability is
                    # agent_factory (not registered for automation/batch/plan
                    # execution etc.); the automation_run check here is purely
                    # a defensive fallback — same as the adjacent batch_plan
                    # block's "mostly defensive". Note that batch_plan's
                    # `_stream_unattended` cannot be reused: it includes
                    # disable_batch_plan (default True for ordinary chats,
                    # which merely lack the batch UI) and would misclassify an
                    # interactive chat as unattended.
                    if (
                        tool_name == "enter_plan_mode"
                        and not context.get("automation_run")
                        and enter_plan_args.get("task_description")
                    ):
                        yield {
                            "type": "plan_redirect",
                            "task_description": str(enter_plan_args.get("task_description", "")),
                        }
                        # Abort the agent loop — the tool description already tells the LLM to hand over control; this enforces it.
                        break

                    # ── Batch execution: pause flow on batch_plan success ──
                    # When the LLM calls the batch_plan MCP tool we treat its
                    # result as a "human-in-the-loop" gate: emit a structured
                    # batch_confirm SSE event with the plan summary, then
                    # terminate the agent loop so the user can review/edit
                    # the prompt template before any item is executed.
                    #
                    # In unattended modes (plan exec / automation) we don't
                    # pause — there's no UI to confirm. We already filter
                    # batch_runner out of those modes' toolkit, so this code
                    # path is mostly defensive (if the agent calls it anyway,
                    # we let it proceed with the plan_id text result).
                    if tool_name == "batch_plan" and not _stream_unattended:
                        bp_data = tool_result_json
                        if (
                            isinstance(bp_data, dict)
                            and "result" in bp_data
                            and isinstance(bp_data["result"], dict)
                        ):
                            bp_data = bp_data["result"]
                        if isinstance(bp_data, dict) and bp_data.get("plan_id"):
                            plan_id = bp_data.get("plan_id")
                            ctx_user_id = str(context.get("user_id", "") or "")
                            ctx_chat_id = str(context.get("chat_id", "") or "")
                            # Backfill user_id + chat_id on the plan: the MCP
                            # subprocess doesn't know who the caller is, so it
                            # creates the plan as 'anonymous'. Patch it now
                            # using the workflow's own context.
                            try:
                                from core.db.engine import SessionLocal
                                from core.db.models import BatchPlan

                                if ctx_user_id:
                                    with SessionLocal() as _db:
                                        _plan = (
                                            _db.query(BatchPlan)
                                            .filter(BatchPlan.plan_id == plan_id)
                                            .first()
                                        )
                                        if _plan and _plan.user_id in ("anonymous", "", None):
                                            _plan.user_id = ctx_user_id
                                            if ctx_chat_id and not _plan.chat_id:
                                                _plan.chat_id = ctx_chat_id
                                            _db.commit()
                                            logger.info(
                                                "[batch] plan %s reassigned to user=%s chat=%s",
                                                plan_id,
                                                ctx_user_id,
                                                ctx_chat_id,
                                            )
                            except Exception as patch_err:
                                logger.warning(
                                    "[batch] failed to backfill plan %s owner: %s",
                                    plan_id,
                                    patch_err,
                                )

                            yield {
                                "type": "batch_confirm",
                                "plan_id": plan_id,
                                "total": bp_data.get("total"),
                                "preview": bp_data.get("preview", []),
                                "default_template": bp_data.get("default_template", ""),
                                "placeholder_keys": bp_data.get("placeholder_keys", []),
                                "source_type": bp_data.get("source_type"),
                                "warnings": bp_data.get("warnings", []),
                                "chat_id": ctx_chat_id if ctx_chat_id else None,
                            }
                            # Stop the agent loop — user must confirm before
                            # any item executes. The MCP tool description
                            # already instructs the LLM not to continue, but
                            # we enforce it here defensively.
                            break

                elif event_type == "heartbeat":
                    yield {"type": "heartbeat"}

                elif event_type == "tool_pending":
                    yield {"type": "tool_pending", **(payload or {})}

                elif event_type == "subagent_event":
                    # Bypass channel for the sub-agent's internal
                    # thinking/tool_call/tool_result/content — attached under
                    # the call_subagent tool card that launched it (linked via
                    # parent_tool_id).
                    nested_citations = _capture_nested_ontology_evidence(
                        payload or {},
                        _ontology_trace,
                        all_citations,
                        citation_offsets,
                    )
                    yield {
                        "type": "subagent_event",
                        **(payload or {}),
                        **({"citations": nested_citations} if nested_citations else {}),
                    }

                elif event_type in ("file_confirm", "design_pick"):
                    # Confirmation-type events (§13 MySpace write confirm /
                    # site-design pick-one-of-three): a tool coroutine has
                    # suspended waiting for the user's out-of-band action.
                    # Pass through to the frontend to show the confirmation
                    # card; the agent task stays blocked in that tool and this
                    # SSE stream does not end — after the out-of-band
                    # POST /file-confirm the tool resumes in place.
                    yield {"type": event_type, **(payload or {})}

                elif event_type == "error":
                    # payload may be a real exception object (kind=="err") or a
                    # dict (e.g. ExceedMaxIters mapped to {"kind":..,"name":..}).
                    # Raising the latter directly gives a TypeError, masking
                    # the real situation — wrap it in a RuntimeError.
                    if isinstance(payload, BaseException):
                        raise payload
                    raise RuntimeError(str(payload))

        except BaseException:
            # A normal first draft keeps its clients so ontology remediation
            # can continue the same ReAct state with tools enabled.
            raise

    except Exception as e:
        import traceback

        logger.error("stream_workflow_error: %s\n%s", e, traceback.format_exc())
        warnings.append(f"Streaming error: {str(e)[:200]}")

        if displayed_tools and not full_response:
            fallback_msg = (
                "抱歉，我在整理工具调用的结果时遇到了问题。以上是已获取的工具执行结果，请参考。"
            )
            full_response = fallback_msg
            yield {"type": "content", "event": "ai_message", "delta": fallback_msg}
        elif not full_response:
            if "streaming_agent" in locals():
                await streaming_agent.shutdown()
                _persistent_clients.append((streaming_agent, list(mcp_clients)))
            raise

    pending_ontology_events = _ontology_runtime.get("runtime_events", [])[_ontology_event_cursor:]
    for ontology_event in pending_ontology_events:
        yield dict(ontology_event)
    _ontology_event_cursor += len(pending_ontology_events)

    _ontology_review_owner_id = _ontology_review_owner(_ontology_runtime, context)
    _ontology_review_claimed = bool(
        full_response and claim_output_review(_ontology_runtime, owner=_ontology_review_owner_id)
    )
    if _ontology_review_claimed:
        from orchestration.subagents.ontology_reviewer import review_ontology_output

        yield {
            "type": "ontology_review",
            "status": "started",
            "level": _ontology_runtime.get("review_level", "checkpoint"),
            **_ontology_review_event_context(_ontology_runtime),
        }
        repair_event_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        async def _remediate(payload: Dict[str, Any]) -> str:
            nonlocal _ontology_event_cursor
            repaired, _, cursor, _ = await _run_ontology_repair_round(
                streaming_agent=streaming_agent,
                context=context,
                payload=payload,
                runtime=_ontology_runtime,
                trace=_ontology_trace,
                citations=all_citations,
                citation_offsets=citation_offsets,
                event_cursor=_ontology_event_cursor,
                event_sink=repair_event_queue.put,
            )
            _ontology_event_cursor = cursor
            return repaired

        review_task = asyncio.create_task(
            review_ontology_output(
                task=user_message,
                answer=full_response,
                runtime=_ontology_runtime,
                trace=_ontology_trace,
                citations=all_citations,
                user_id=str(context.get("user_id", "")),
                chat_id=context.get("chat_id"),
                model_name=str(context.get("model_name", "") or "") or None,
                model_provider_id=str(context.get("model_provider_id", "") or "") or None,
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
        except asyncio.CancelledError:
            if not review_task.done():
                review_task.cancel()
            release_output_review(_ontology_runtime, owner=_ontology_review_owner_id)
            await streaming_agent.shutdown()
            _persistent_clients.append((streaming_agent, list(mcp_clients)))
            raise
        except Exception as exc:  # noqa: BLE001
            if not review_task.done():
                review_task.cancel()
            review = _ontology_review_failure_result(full_response, exc)
        complete_output_review(
            _ontology_runtime,
            owner=_ontology_review_owner_id,
            verdict=str(review.get("verdict") or "unknown"),
            attempts=int(review.get("attempts") or 1),
        )
        _ontology_runtime.setdefault("output_review", {}).update(
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
            "level": _ontology_runtime.get("review_level", "checkpoint"),
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
            **_ontology_review_event_context(_ontology_runtime),
        }

    await streaming_agent.shutdown()
    _persistent_clients.append((streaming_agent, list(mcp_clients)))

    yield {
        "type": "meta",
        "route": "main",
        "is_markdown": _looks_markdown(full_response),
        "sources": _resolve_sources_conflict([]),
        "artifacts": [],
        "warnings": warnings,
        "citations": all_citations,
        "usage": streaming_agent.get_usage(),
        "ontology_governance": _ontology_governance_summary(_ontology_runtime),
    }

    # ── [memory] Post-response pipeline (SSE already closed, user isn't waiting) ──
    # No memory is written unless the user opted into memory_write_enabled (first gate).
    # Everything goes through memory_pipeline.schedule_post_response_tasks:
    # global Semaphore + 4 extractors + sanitize + write L1/L2/Session + audit,
    # all executed in the background with bounds.
    if _mem0_write_enabled:
        logger.info(
            "[memory] schedule post-response: full_response_len=%s, user=%s ws=%s",
            len(full_response) if full_response else 0,
            _mem0_user_id,
            _mem0_workspace_id,
        )
        save_memories_background(
            _mem0_user_id,
            user_message,
            full_response,
            _mem0_write_enabled,
            workspace_id=_mem0_workspace_id,
            chat_id=_mem0_chat_id,
            scope_user_id=_mem0_scope_user_id,
        )
    else:
        logger.debug("[workflow] memory save skipped: write_enabled=False (user=%s)", _mem0_user_id)
