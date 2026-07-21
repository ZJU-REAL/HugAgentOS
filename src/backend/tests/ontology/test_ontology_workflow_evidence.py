from __future__ import annotations

from types import SimpleNamespace

import pytest
from orchestration.workflow import (
    _astream_subagent_direct,
    _capture_nested_ontology_evidence,
    _ontology_repair_prompt,
    _ontology_review_failure_result,
    _run_ontology_repair_round,
    astream_chat_workflow,
)


def test_repair_prompt_requires_polished_delivery_instead_of_validation_report():
    prompt = _ontology_repair_prompt(
        {
            "original_task": "只输出一句润色后的风险说明，并保存到 Word。",
            "answer": "企业风险极高。",
            "violations": [{"rule_id": "risk_report_requires_evidence"}],
        }
    )

    assert "只输出一句润色后的风险说明，并保存到 Word。" in prompt
    assert "待修订原始输出" in prompt
    assert "企业风险极高。" in prompt
    assert "最终交付必须是利用核验结论直接修正后的完整润色稿" in prompt
    assert "证据核对、逐项判定、原文对比和修改说明都属于内部校验过程" in prompt
    assert "以待修订原始输出为底稿做最小必要修改" in prompt
    assert "不要因为查到更多资料就加入与原主张无关" in prompt
    assert "用户只要求一句话时仍输出一句话" in prompt
    assert "绝不能反向断言为‘不存在’" in prompt
    assert "必须在内部逐条检查‘确定性违规’中的每个条件" in prompt
    assert "把完全相同的正文写入用户要求的文件" in prompt
    assert "已经激活的 enforce 级领域本体约束及本修复指令" in prompt
    assert "若‘确定性违规’包含‘未调用’某些必需工具" in prompt
    assert "再次原样输出待修订内容，均视为修复失败" in prompt


def test_streaming_workflows_do_not_shadow_global_asyncio_import():
    assert "asyncio" not in astream_chat_workflow.__code__.co_varnames
    assert "asyncio" not in _astream_subagent_direct.__code__.co_varnames


def test_review_failure_preserves_original_and_escalates_instead_of_aborting():
    result = _ontology_review_failure_result("已经完成的原文", RuntimeError("review failed"))

    assert result["answer"] == "已经完成的原文"
    assert result["verdict"] == "escalate"
    assert result["revised"] is False
    assert result["manual_review"]["required"] is True


def test_nested_subagent_tool_results_join_outer_trace_and_citations():
    trace = []
    citations = []
    offsets = {}
    tools = [
        (
            "search_company",
            {
                "items": [
                    {
                        "企业名称": "杭州量知数据科技有限公司",
                        "企业状态": "存续",
                    }
                ]
            },
        ),
        ("get_company_base_info", {"企业名称": "杭州量知数据科技有限公司"}),
        ("get_company_risk_warning", {"经营司法风险": {"风险总数": 0}}),
    ]

    for index, (tool_name, output) in enumerate(tools, 1):
        extracted = _capture_nested_ontology_evidence(
            {
                "sub_type": "tool_result",
                "tool_name": tool_name,
                "tool_id": f"child-tool-{index}",
                "output": output,
                "status": "success",
                "agent_id": "risk-agent",
                "sub_run_id": "sub-run-1",
                "parent_tool_id": "call-subagent-1",
            },
            trace,
            citations,
            offsets,
        )
        assert extracted

    assert {item["tool_name"] for item in trace} == {
        "search_company",
        "get_company_base_info",
        "get_company_risk_warning",
    }
    assert all(item["source"] == "subagent" for item in trace)
    assert {item["tool_name"] for item in citations} == {
        "search_company",
        "get_company_base_info",
        "get_company_risk_warning",
    }
    assert {item["tool_id"] for item in citations} == {
        "child-tool-1",
        "child-tool-2",
        "child-tool-3",
    }


@pytest.mark.asyncio
async def test_repair_round_continues_agent_and_streams_candidate_after_draft():
    runtime = {"enabled": True, "runtime_events": []}

    class FakeStreamingAgent:
        def __init__(self):
            self.agent = SimpleNamespace(state=SimpleNamespace(ontology_runtime=runtime))
            self.messages = None

        async def stream(self, messages, context):
            self.messages = messages
            yield "tool_pending", {"reason": "tool_args_streaming"}
            yield "tool_call", {
                "name": "get_company_risk_warning",
                "id": "repair-tool-1",
                "args": {"company_id": "company-1"},
            }
            yield "tool_result", {
                "name": "get_company_risk_warning",
                "id": "repair-tool-1",
                "content": '{"经营司法风险":{"风险总数":0}}',
            }
            yield "thinking_delta", "正在核对新增证据"
            yield "text_delta", "<ontology_revision>修复后的"
            yield "text_delta", "完整答案</ontology_revision>"

    streaming_agent = FakeStreamingAgent()
    trace = []
    citations = []
    streamed_events = []

    async def _capture_event(event):
        streamed_events.append(event)

    answer, events, cursor, tool_count = await _run_ontology_repair_round(
        streaming_agent=streaming_agent,
        context={"ontology_runtime": runtime},
        payload={
            "attempt": 1,
            "source": "deterministic",
            "original_task": "请生成 Word 格式的企业风险分析报告",
            "violations": [{"rule_id": "risk_report_requires_evidence"}],
            "citations": [],
        },
        runtime=runtime,
        trace=trace,
        citations=citations,
        citation_offsets={},
        event_cursor=0,
        event_sink=_capture_event,
    )

    assert answer == "修复后的完整答案"
    assert tool_count == 1
    assert cursor == 0
    assert streaming_agent.messages[0]["role"] == "user"
    assert "同一任务唯一一次领域本体修复" in streaming_agent.messages[0]["content"]
    assert "请生成 Word 格式的企业风险分析报告" in streaming_agent.messages[0]["content"]
    assert "不得降级为纯文字替代" in streaming_agent.messages[0]["content"]
    assert any(item.get("tool_name") == "get_company_risk_warning" for item in trace)
    assert citations[0]["id"] == "get_company_risk_warning-1"
    assert any(item.get("type") == "ontology_revision_thinking" for item in streamed_events)
    pending_event = next(item for item in streamed_events if item.get("type") == "tool_pending")
    assert pending_event["reason"] == "tool_args_streaming"
    assert pending_event["scope"] == "ontology_revision"
    repair_tool_events = [
        item for item in streamed_events if item.get("type") in {"tool_call", "tool_result"}
    ]
    assert repair_tool_events
    assert all(item.get("scope") == "ontology_revision" for item in repair_tool_events)
    assert streamed_events[0] == {
        "type": "ontology_repair",
        "status": "started",
        "attempt": 1,
        "source": "deterministic",
    }
    assert streamed_events[-1]["type"] == "ontology_repair"
    assert streamed_events[-1]["status"] == "completed"
    content_events = [item for item in streamed_events if item.get("type") == "ontology_revision"]
    assert "".join(item["delta"] for item in content_events) == "修复后的完整答案"
    assert not any(item.get("type") == "content_replace" for item in events)


@pytest.mark.asyncio
async def test_repair_round_ignores_inline_wrapper_example_before_real_revision():
    runtime = {"enabled": True, "runtime_events": []}

    class FakeStreamingAgent:
        def __init__(self):
            self.agent = SimpleNamespace(state=SimpleNamespace(ontology_runtime=runtime))

        async def stream(self, messages, context):
            yield "text_delta", "正文应放在 `<ontology_revision>...</ontology_revision>` 中。"
            yield "text_delta", "</think>\n\n<ontology_revision>\n"
            yield "text_delta", "这是包含真实证据与完整结论的修订正文。"
            yield "text_delta", "\n</ontology_revision>"

    streamed_events = []

    async def _capture_event(event):
        streamed_events.append(event)

    answer, _, _, _ = await _run_ontology_repair_round(
        streaming_agent=FakeStreamingAgent(),
        context={"ontology_runtime": runtime},
        payload={"attempt": 1, "source": "deterministic", "citations": []},
        runtime=runtime,
        trace=[],
        citations=[],
        citation_offsets={},
        event_cursor=0,
        event_sink=_capture_event,
    )

    assert answer == "这是包含真实证据与完整结论的修订正文。"
    content = "".join(
        item.get("delta", "") for item in streamed_events if item.get("type") == "ontology_revision"
    )
    thinking = "".join(
        item.get("delta", "")
        for item in streamed_events
        if item.get("type") == "ontology_revision_thinking"
    )
    assert content.strip() == answer
    assert "<ontology_revision>...</ontology_revision>" in thinking


@pytest.mark.asyncio
async def test_repair_round_streams_revision_adjacent_to_reasoning_close_tag():
    runtime = {"enabled": True, "runtime_events": []}

    class FakeStreamingAgent:
        def __init__(self):
            self.agent = SimpleNamespace(state=SimpleNamespace(ontology_runtime=runtime))

        async def stream(self, messages, context):
            yield "text_delta", "已完成证据核验，开始输出修订稿。</think><ontology_re"
            yield "text_delta", "vision>第一段修订正文包含充分事实依据和明确结论，"
            yield "text_delta", "第二段继续说明证据边界并给出审慎判断。"
            yield "text_delta", "</ontology_revision>"

    streamed_events = []

    async def _capture_event(event):
        streamed_events.append(event)

    answer, _, _, _ = await _run_ontology_repair_round(
        streaming_agent=FakeStreamingAgent(),
        context={"ontology_runtime": runtime},
        payload={"attempt": 1, "source": "review", "citations": []},
        runtime=runtime,
        trace=[],
        citations=[],
        citation_offsets={},
        event_cursor=0,
        event_sink=_capture_event,
    )

    content_events = [
        item for item in streamed_events if item.get("type") == "ontology_revision"
    ]
    thinking = "".join(
        item.get("delta", "")
        for item in streamed_events
        if item.get("type") == "ontology_revision_thinking"
    )

    assert answer == (
        "第一段修订正文包含充分事实依据和明确结论，"
        "第二段继续说明证据边界并给出审慎判断。"
    )
    assert len(content_events) >= 2
    assert "".join(item["delta"] for item in content_events) == answer
    assert "第一段修订正文" not in thinking
    assert "<ontology_revision>" not in answer
    assert "</ontology_revision>" not in answer
