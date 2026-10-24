"""Re-run the originating agent to produce a revision when ontology review rejects an answer."""

from __future__ import annotations
from core.llm.citations.allocator import collect_citation_dicts

import json
import logging
import typing as _typing
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import core.agent_runtime.events as agent_runtime_events
import core.agent_runtime.integrations.ontology_evidence as ontology_evidence
import core.config.display_names as display_names
import core.ontology.revision as revision
from core.llm.citations.allocator import AnchorAllocator
from core.llm.runtime.streaming import StreamingAgent

logger = logging.getLogger(__name__)


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
        "最低长度，终稿必须达到该长度；若要求引用，相关事实后必须包含真实引用标记"
        "（从工具结果复制 cite_id，写成 `[锚文本](cite:eN)`）"
        "且按规则补齐参考资料；若要求区分事实、推断和待核验项，可在同一句中用分号和简短标签"
        "表达，不得省略。先确定唯一的合格终稿，再把完全相同的正文写入用户要求的文件，"
        "不得先生成文件后又输出一份更短、缺引用或结论不同的候选正文。"
        "思考过程只放在模型的 thinking/reasoning 通道。最终正文必须且只能放在"
        " `<ontology_revision>完整候选答案</ontology_revision>` 中。"
        "在正式输出最终正文前，不要复述、引用或示例化这组标签，也不要用省略号代替正文。"
        "标签内必须是可独立阅读、内容完整的修订答案；若无法修订，则原样放入当前完整答案。"
        "事实、推断和待核验项必须明确区分；引用已有或新增工具结果时，把工具结果里"
        "标注的 cite_id 原样复制进 `[锚文本](cite:eN)`，禁止自行编号。\n"
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
    allocator: Optional[AnchorAllocator] = None,
    event_cursor: int = 0,
    subagent_log_id: Optional[str] = None,
    event_sink: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Tuple[str, List[Dict[str, Any]], int, int]:
    """Continue the originating agent and stream a separate revision candidate."""
    answer = ""
    events: _typing.List[_typing.Dict[str, _typing.Any]] = []
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
                event_type, event_payload = await agent_runtime_events._anext_in_subagent_log_scope(
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

        if event_type == "vision_progress":
            await _publish({"type": "vision_progress", **(event_payload or {})})
            continue

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
                {
                    "type": "ontology_revision_thinking",
                    "delta": str(event_payload or ""),
                }
            )
        elif event_type == "tool_call_start":
            tool_name = str(event_payload.get("name") or "unknown")
            if tool_name != "update_plan":
                await _publish(
                    {
                        "type": "tool_call_start",
                        "tool_name": tool_name,
                        "tool_display_name": display_names.TOOL_DISPLAY_NAMES.get(
                            tool_name, tool_name
                        ),
                        "tool_id": str(event_payload.get("id") or ""),
                        "scope": "ontology_revision",
                    }
                )
        elif event_type == "tool_call_delta":
            tool_name = str(event_payload.get("name") or "unknown")
            if tool_name != "update_plan" and event_payload.get("delta"):
                await _publish(
                    {
                        "type": "tool_call_delta",
                        "tool_name": tool_name,
                        "tool_id": str(event_payload.get("id") or ""),
                        "arguments_delta": str(event_payload.get("delta") or ""),
                        "scope": "ontology_revision",
                    }
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
                    "input": ontology_evidence._bounded_trace_value(tool_args),
                    "source": "ontology_repair",
                }
            )
            await _publish(
                {
                    "type": "tool_call",
                    "tool_name": tool_name,
                    "tool_display_name": display_names.TOOL_DISPLAY_NAMES.get(tool_name, tool_name),
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
            result = ontology_evidence._parse_tool_result_value(event_payload.get("content"))
            cit_dicts = collect_citation_dicts(tool_id, allocator)
            citations.extend(cit_dicts)
            trace.append(
                {
                    "type": "tool_result",
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "result": ontology_evidence._bounded_trace_value(result),
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
            nested_citations = ontology_evidence._capture_nested_ontology_evidence(
                event_payload or {},
                trace,
                citations,
                allocator,
            )
            sub_type = str((event_payload or {}).get("sub_type") or "")
            if sub_type in {
                "start",
                "thinking",
                "content",
                "tool_call",
                "tool_call_delta",
                "tool_result",
                "end",
            }:
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
        fallback = revision.normalize_revision_candidate(raw_text.rsplit("</think>", 1)[-1])
        if revision.is_substantive_revision(fallback):
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
    return revision.normalize_revision_candidate(answer), events, event_cursor, tool_count
