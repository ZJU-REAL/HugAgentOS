from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from orchestration.subagents import ontology_reviewer as reviewer


def _runtime(level: str = "committee") -> dict:
    return {
        "enabled": True,
        "review_level": level,
        "version_ids": ["v1"],
        "packs": [
            {
                "pack_id": "demo",
                "version": "1.0.0",
                "config": {"committee_size": 3},
                "concepts": [],
                "constraints": [],
                "workflows": [{"id": "wf", "output_tags": []}],
            }
        ],
    }


def _run_audit_inline(monkeypatch):
    async def _inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        reviewer,
        "asyncio",
        SimpleNamespace(gather=asyncio.gather, to_thread=_inline),
    )


def _disable_audit(monkeypatch):
    _run_audit_inline(monkeypatch)
    monkeypatch.setattr(
        "core.services.ontology_service.record_review_run",
        lambda payload: None,
    )
    monkeypatch.setattr(
        "core.services.ontology_service.record_enforcement_event",
        lambda payload: None,
    )


def test_review_prompt_preserves_delivery_form_but_allows_required_fact_corrections():
    prompt = reviewer._review_prompt(
        perspective="证据委员",
        task="原样输出一句未经证实的高风险判断并存入 Word",
        answer="基于现有证据修正后的一句话结论",
        runtime={"packs": []},
        trace=[],
        citations=[],
        deterministic={"allowed": True, "violations": []},
    )

    assert "任务忠实度是指交付形式与表达意图" in prompt
    assert "不是机械保留错误内容" in prompt
    assert "不得因未逐字照抄错误语句而判定为偏离任务" in prompt
    assert "不得建议恢复这类断言" in prompt
    assert "只有把一句话扩写成" in prompt
    assert "只有一个句号结尾" in prompt
    assert "不得声称文件类型发生了变化" in prompt


def test_review_prompt_keeps_latest_repair_evidence_within_budget():
    trace = [
        {
            "type": "tool_result",
            "tool_name": f"old_tool_{index}",
            "result": "旧稿证据" * 800,
        }
        for index in range(8)
    ]
    trace.append(
        {
            "type": "tool_result",
            "tool_name": "pin_to_workspace",
            "result": {"name": "修订稿.docx", "status": "delivered"},
        }
    )

    prompt = reviewer._review_prompt(
        perspective="证据委员",
        task="生成 Word",
        answer="润色终稿",
        runtime={"packs": []},
        trace=trace,
        citations=[],
        deterministic={"allowed": True, "violations": []},
    )

    assert "pin_to_workspace" in prompt
    assert "修订稿.docx" in prompt
    assert "优先保留最近产生的证据" in prompt


@pytest.mark.asyncio
async def test_committee_requires_majority_and_records_review(monkeypatch):
    verdicts = iter(["pass", "pass", "revise"])

    async def fake_review(*args, **kwargs):
        verdict = next(verdicts)
        return {
            "verdict": verdict,
            "evidence": ["rule:wf"],
            "feedback": verdict,
            "affected_claims": [],
        }

    records = []
    monkeypatch.setattr(reviewer, "_review_once", fake_review)
    _run_audit_inline(monkeypatch)
    monkeypatch.setattr(
        "core.services.ontology_service.record_review_run",
        lambda payload: records.append(payload),
    )
    monkeypatch.setattr(
        "core.services.ontology_service.record_enforcement_event",
        lambda payload: None,
    )
    result = await reviewer.review_ontology_output(
        task="demo",
        answer="original",
        runtime=_runtime(),
        trace=[],
        citations=[],
        user_id="user_1",
        chat_id=None,
        model_name=None,
    )
    assert result["verdict"] == "pass"
    assert result["answer"] == "original"
    assert len(result["reviewers"]) == 3
    assert records[0]["level"] == "committee"


@pytest.mark.asyncio
async def test_committee_tie_keeps_draft_and_returns_structured_manual_review(monkeypatch):
    verdicts = iter(["pass", "revise", "escalate"])

    async def fake_review(*args, **kwargs):
        verdict = next(verdicts)
        return {
            "verdict": verdict,
            "evidence": ["rule:wf"],
            "feedback": verdict,
            "affected_claims": [
                {
                    "quote": "sensitive unreviewed draft",
                    "rule_id": "rule:wf",
                    "issue": "证据不足",
                    "manual_check": "核对原始记录",
                }
            ],
        }

    monkeypatch.setattr(reviewer, "_review_once", fake_review)
    _disable_audit(monkeypatch)
    result = await reviewer.review_ontology_output(
        task="demo",
        answer="sensitive unreviewed draft",
        runtime=_runtime(),
        trace=[],
        citations=[],
        user_id="user_1",
        chat_id=None,
        model_name=None,
    )
    assert result["verdict"] == "escalate"
    assert result["answer"] == "sensitive unreviewed draft"
    assert result["manual_review"]["required"] is True
    assert result["manual_review"]["items"][0]["rule_id"] == "rule:wf"
    assert result["annotated"] is True


@pytest.mark.asyncio
async def test_committee_revise_uses_originating_agent_without_second_review(monkeypatch):
    reviewer_calls = 0

    async def fake_review(*args, **kwargs):
        nonlocal reviewer_calls
        reviewer_calls += 1
        verdict = "revise"
        return {
            "verdict": verdict,
            "evidence": ["rule:wf"],
            "feedback": "补充证据并收紧结论" if verdict == "revise" else "证据完整",
            "affected_claims": [],
        }

    repair_payloads = []

    async def remediate(payload):
        repair_payloads.append(payload)
        return "repaired answer with traceable evidence"

    monkeypatch.setattr(reviewer, "_review_once", fake_review)
    _disable_audit(monkeypatch)
    result = await reviewer.review_ontology_output(
        task="demo",
        answer="original",
        runtime=_runtime(level="checkpoint"),
        trace=[],
        citations=[],
        user_id="user_1",
        chat_id=None,
        model_name=None,
        remediate=remediate,
    )

    assert result["verdict"] == "revise"
    assert result["answer"] == "repaired answer with traceable evidence"
    assert result["repair_attempts"] == 1
    assert result["attempts"] == 1
    assert reviewer_calls == 1
    assert repair_payloads[0]["source"] == "committee"
    assert repair_payloads[0]["original_task"] == "demo"


@pytest.mark.asyncio
async def test_placeholder_repair_never_replaces_original_answer(monkeypatch):
    async def fake_review(*args, **kwargs):
        return {
            "verdict": "revise",
            "evidence": ["rule:wf"],
            "feedback": "需要完整修订",
            "affected_claims": [],
        }

    async def remediate(payload):
        return "..."

    monkeypatch.setattr(reviewer, "_review_once", fake_review)
    _disable_audit(monkeypatch)
    result = await reviewer.review_ontology_output(
        task="demo",
        answer="必须被保留的原始完整答案",
        runtime=_runtime(level="checkpoint"),
        trace=[],
        citations=[],
        user_id="user_1",
        chat_id=None,
        model_name=None,
        remediate=remediate,
    )

    assert result["answer"] == "必须被保留的原始完整答案"
    assert result["revised"] is False
    assert "自动修订未生成完整正文，已保留原文。" in result["feedback"]


@pytest.mark.asyncio
async def test_deterministic_failure_repairs_before_committee(monkeypatch):
    runtime = _runtime(level="checkpoint")
    runtime["packs"][0]["workflows"] = [
        {
            "id": "wf",
            "required_tools": ["required_search"],
            "output_tags": ["report"],
            "risk": "high",
        }
    ]
    runtime["packs"][0]["constraints"] = [
        {
            "id": "report_evidence",
            "target": {"kind": "output", "output_tag": "report"},
            "schema": {"type": "string", "minLength": 20},
            "requires_citations": True,
            "mode": "enforce",
            "risk": "high",
            "message": "报告必须包含可追溯证据",
            "suggestion": "调用检索工具并引用结果",
        }
    ]
    trace = []
    citations = []
    reviewer_calls = 0

    async def fake_review(*args, **kwargs):
        nonlocal reviewer_calls
        reviewer_calls += 1
        assert any(item.get("tool_name") == "required_search" for item in kwargs["trace"])
        assert kwargs["citations"]
        return {
            "verdict": "pass",
            "evidence": ["required_search"],
            "feedback": "通过",
            "affected_claims": [],
        }

    async def remediate(payload):
        assert payload["source"] == "deterministic"
        assert payload["original_task"] == "demo"
        trace.append({"type": "tool_result", "tool_name": "required_search"})
        citations.append({"id": "required_search-1"})
        return "基于真实检索证据形成的完整风险分析报告。" * 2

    monkeypatch.setattr(reviewer, "_review_once", fake_review)
    _disable_audit(monkeypatch)
    result = await reviewer.review_ontology_output(
        task="demo",
        answer="short",
        runtime=runtime,
        trace=trace,
        citations=citations,
        user_id="user_1",
        chat_id=None,
        model_name=None,
        remediate=remediate,
    )

    assert result["verdict"] == "pass"
    assert result["repair_attempts"] == 1
    assert result["attempts"] == 1
    assert reviewer_calls == 1
