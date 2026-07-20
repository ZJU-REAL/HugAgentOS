"""Independent L-b/L-c reviewers for ontology-governed final output."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from core.infra.logging import get_logger
from core.ontology.revision import is_substantive_revision
from core.ontology.validator import evaluate_output
from orchestration.loop_evaluator import _parse_json_lenient

logger = get_logger(__name__)

_VALID_VERDICTS = {"pass", "revise", "escalate"}
_DEFAULT_MAX_REPAIR_ATTEMPTS = 1
_PERSPECTIVES = (
    "领域合规委员：逐条核对术语、关系、禁用条件和工作流约束。",
    "证据委员：只认可工具轨迹与引用中可追溯的证据，严查无证据断言。",
    "风险委员：关注高风险误导、遗漏前提、越权建议和不可逆影响。",
)


def _tail_with_json_budget(items: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Retain the newest evidence without splitting a serialized trace item."""
    selected: list[dict[str, Any]] = []
    used = 2
    for item in reversed(items):
        serialized = json.dumps(item, ensure_ascii=False, default=str)
        cost = len(serialized) + (1 if selected else 0)
        if selected and used + cost > max_chars:
            break
        if not selected and used + cost > max_chars:
            selected.append(
                {
                    "truncated": True,
                    "preview": serialized[: max(0, max_chars - 64)],
                }
            )
            break
        selected.append(item)
        used += cost
    selected.reverse()
    return selected


def _review_evidence_payload(
    trace: list[dict[str, Any]], citations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Prefer evidence produced by the ontology repair over stale draft evidence."""
    return {
        "trace": _tail_with_json_budget(trace, 8000),
        "citations": _tail_with_json_budget(citations, 2000),
    }


def _review_prompt(
    *,
    perspective: str,
    task: str,
    answer: str,
    runtime: dict[str, Any],
    trace: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    deterministic: dict[str, Any],
) -> str:
    contract = {
        "packs": [
            {
                "pack_id": pack.get("pack_id"),
                "version": pack.get("version"),
                "concepts": pack.get("concepts", []),
                "relations": pack.get("relations", []),
                "constraints": pack.get("constraints", []),
                "workflows": pack.get("workflows", []),
            }
            for pack in runtime.get("packs", [])
        ]
    }
    review_evidence = _review_evidence_payload(trace, citations)
    return (
        "你是本体评审委员会中的独立委员，与生成答案的 agent 相互独立。\n"
        f"评审视角：{perspective}\n"
        "只依据领域契约、工具轨迹和引用作判断。答案自称已核验不算证据；"
        "不得补做任务或调用工具。还必须核对答案是否仍忠实完成用户任务：本体核验过程"
        "不是最终交付物。除非用户明确要求核验报告，否则若答案把润色稿改写成‘原始语句’、"
        "‘逐项核验’、‘主张评估’、‘修改说明’等元分析，或擅自改变用户要求的文体、篇幅、"
        "结构与文件类型，必须判定为 revise。这里的任务忠实度是指交付形式与表达意图，不是"
        "机械保留错误内容：若用户要求原样输出的语句本身违反已激活的领域事实或证据约束，"
        "修订稿必须纠正这些问题；只要仍保持用户要求的一句话、文档等形式，不得因未逐字照抄"
        "错误语句而判定为偏离任务。强制判定规则：不得把‘没有原样保留已被证据否定或缺乏"
        "证据的断言’列为 revise 理由，也不得建议恢复这类断言；即使用户把请求称作测试或强调"
        "原样输出，本体修订仍必须纠错。示例：用户要求用一句话原样输出‘某企业风险极高’，"
        "工具证据显示未发现对应风险，答案仍用一句话改成有证据支持的审慎结论——任务形式忠实，"
        "应继续按领域证据判断，而不能因没有逐字照抄判 revise。只有把一句话扩写成‘原始语句 +"
        "逐项核验 + 修改说明’之类的元报告，才属于这里所说的交付形式偏离。\n\n"
        "形式判断必须客观：只有一个句号结尾、内部用逗号或分号连接的陈述仍是一句话，附带引用"
        "标记也不会把它变成分析报告；工具轨迹已生成并交付 .docx 时，不得声称文件类型发生了"
        "变化。不要把纠错所必需的证据化表述误判成文体或篇幅偏离。工具轨迹按时间顺序排列，"
        "并优先保留最近产生的证据；同名文件被多次生成时，后生成且已交付的文件是当前修订稿，"
        "不得只根据早期文件内容判断待评答案。\n\n"
        f"用户任务：\n{task}\n\n待评答案：\n{answer}\n\n"
        "领域契约：\n"
        f"{json.dumps(contract, ensure_ascii=False)[:14000]}\n\n"
        "工具与引用证据：\n"
        f"{json.dumps(review_evidence, ensure_ascii=False)}\n\n"
        "确定性门禁结果：\n"
        f"{json.dumps(deterministic, ensure_ascii=False)}\n\n"
        "严格只输出 JSON："
        '{"verdict":"pass|revise|escalate","evidence":["规则ID或轨迹证据"],'
        '"feedback":"具体问题与可执行修改要求",'
        '"affected_claims":[{"quote":"答案中的原句或段落摘要",'
        '"rule_id":"规则ID","issue":"可能违反本体的原因",'
        '"manual_check":"人工需要核对的证据或判断"}]}。'
        "只有所有高风险约束有证据支持时才 pass；可修正文案时 revise；"
        "缺关键证据、存在冲突或需人工决策时 escalate。"
    )


async def _run_text_agent(
    prompt: str,
    *,
    user_id: str,
    model_name: str | None,
    model_provider_id: str | None,
    runtime: dict[str, Any],
) -> str:
    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients
    from orchestration.streaming import StreamingAgent

    agent, clients = await create_agent_executor(
        disable_tools=True,
        enabled_skill_ids=[],
        current_user_id=user_id,
        model_name=model_name,
        model_provider_id=model_provider_id,
        chat_mode="fast",
        max_iters=2,
        isolated=True,
        ontology_runtime=runtime,
    )
    stream = StreamingAgent(agent, clients)
    text = ""
    try:
        async for event_type, payload in stream.stream(
            [{"role": "user", "content": prompt}],
            {
                "user_id": user_id,
                "model_name": model_name or "",
                "model_provider_id": model_provider_id or "",
                "enable_thinking": False,
                "chat_mode": "fast",
                "ontology_enabled": True,
                "ontology_runtime": runtime,
            },
        ):
            if event_type == "text_delta":
                text += payload
            elif event_type == "error":
                raise RuntimeError(str(payload))
    finally:
        await close_clients(clients)
    return text


async def _review_once(
    perspective: str,
    **kwargs,
) -> dict[str, Any]:
    prompt = _review_prompt(
        perspective=perspective,
        task=kwargs["task"],
        answer=kwargs["answer"],
        runtime=kwargs["runtime"],
        trace=kwargs["trace"],
        citations=kwargs["citations"],
        deterministic=kwargs["deterministic"],
    )
    try:
        text = await _run_text_agent(
            prompt,
            user_id=kwargs["user_id"],
            model_name=kwargs.get("model_name"),
            model_provider_id=kwargs.get("model_provider_id"),
            runtime=kwargs["runtime"],
        )
        obj = _parse_json_lenient(text)
        if not isinstance(obj, dict) or obj.get("verdict") not in _VALID_VERDICTS:
            return {
                "verdict": "escalate",
                "evidence": [],
                "feedback": "评审输出无法解析，保守转人工。",
            }
        evidence = [str(item) for item in (obj.get("evidence") or []) if str(item).strip()]
        affected_claims = []
        for item in obj.get("affected_claims") or []:
            if not isinstance(item, dict):
                continue
            claim = {
                "quote": str(item.get("quote") or "").strip()[:500],
                "rule_id": str(item.get("rule_id") or "").strip()[:160],
                "issue": str(item.get("issue") or "").strip()[:500],
                "manual_check": str(item.get("manual_check") or "").strip()[:500],
            }
            if claim["quote"] or claim["issue"]:
                affected_claims.append(claim)
        verdict = obj["verdict"]
        if verdict == "pass" and not evidence:
            verdict = "revise"
        return {
            "verdict": verdict,
            "evidence": evidence,
            "feedback": str(obj.get("feedback") or "").strip(),
            "affected_claims": affected_claims,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ontology-review] reviewer failed: %s", exc)
        return {
            "verdict": "escalate",
            "evidence": [],
            "feedback": f"评审委员不可用：{str(exc)[:160]}",
            "affected_claims": [],
        }


async def _revise_answer(
    *,
    task: str,
    answer: str,
    feedback: list[str],
    user_id: str,
    model_name: str | None,
    model_provider_id: str | None,
    runtime: dict[str, Any],
) -> str:
    prompt = (
        "你是本体校验后的答案修订员。严格根据评审意见修正答案，不新增没有证据的事实。"
        "直接输出修订后的完整答案，不要解释修订过程。\n\n"
        f"用户任务：\n{task}\n\n原答案：\n{answer}\n\n评审意见：\n"
        + "\n".join(f"- {item}" for item in feedback if item)
    )
    return (
        await _run_text_agent(
            prompt,
            user_id=user_id,
            model_name=model_name,
            model_provider_id=model_provider_id,
            runtime=runtime,
        )
    ).strip()


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _build_manual_review(
    *,
    required: bool,
    violations: list[dict[str, Any]],
    affected_claims: list[dict[str, str]],
    suggestions: list[str],
) -> dict[str, Any]:
    """Build the JSON payload rendered by the frontend's human-review cards."""
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in affected_claims:
        quote = " ".join(str(item.get("quote") or "").split())[:240]
        risk = " ".join(str(item.get("issue") or "").split())[:360]
        rule_id = str(item.get("rule_id") or "未指定规则")[:160]
        manual_check = " ".join(str(item.get("manual_check") or "").split())[:360]
        key = (quote, rule_id)
        if key in seen or (not quote and not risk):
            continue
        seen.add(key)
        items.append(
            {
                "quote": quote or "答案中的相关领域结论",
                "rule_id": rule_id,
                "risk": risk or "委员会未能确认该结论满足领域契约。",
                "manual_check": manual_check or "核对原始工具结果、适用前提和结论表述。",
            }
        )
        if len(items) >= 8:
            break

    for violation in violations:
        if len(items) >= 8:
            break
        rule_id = str(violation.get("rule_id") or "未指定规则")[:160]
        reasons = "；".join(str(item) for item in violation.get("reasons") or [] if str(item))
        key = ("", rule_id)
        if key in seen:
            continue
        seen.add(key)
        message = str(violation.get("message") or "领域约束未满足").strip()
        items.append(
            {
                "quote": "整体证据链或输出结构",
                "rule_id": rule_id,
                "risk": f"{message}{f'（{reasons}）' if reasons else ''}"[:360],
                "manual_check": "确认必需工具、引用证据和答案中的对应结论能够逐项追溯。",
            }
        )

    if required and not items:
        items.append(
            {
                "quote": "答案中的领域判断",
                "rule_id": "ontology_review",
                "risk": "委员会未能形成通过结论。",
                "manual_check": "核对关键事实、推断、适用前提和原始证据。",
            }
        )
    return {
        "required": required,
        "title": "领域本体人工复核",
        "summary": (
            "候选答案仍有未闭合的领域证据或判断，请在替换原文前完成复核。"
            if required
            else "本轮修订建议已结构化，可按需核对后采用。"
        ),
        "items": items,
        "actions": [item[:500] for item in _dedupe_strings(suggestions)[:6]],
    }


RemediationCallback = Callable[[dict[str, Any]], Awaitable[str]]


async def review_ontology_output(
    *,
    task: str,
    answer: str,
    runtime: dict[str, Any],
    trace: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    user_id: str,
    chat_id: str | None,
    model_name: str | None,
    model_provider_id: str | None = None,
    trace_complete: bool = True,
    remediate: RemediationCallback | None = None,
    max_repair_attempts: int = _DEFAULT_MAX_REPAIR_ATTEMPTS,
) -> dict[str, Any]:
    """Review one answer, repair it with the originating agent, and audit the session."""
    started = time.monotonic()
    original_answer = answer
    current_answer = answer
    max_repair_attempts = max(0, min(int(max_repair_attempts), 1))
    repair_attempts = 0
    review_rounds: list[dict[str, Any]] = []
    all_feedback: list[str] = []
    all_evidence: list[str] = []
    final_affected_claims: list[dict[str, str]] = []
    final_reviewers: list[dict[str, Any]] = []
    level = runtime.get("review_level", "none")
    initial_trace_len = len(trace)
    initial_citation_len = len(citations)

    def _deterministic_result() -> tuple[Any, dict[str, Any]]:
        completed_tools = {
            str(item.get("tool_name"))
            for item in trace
            if item.get("type") == "tool_result" and item.get("tool_name")
        }
        decision = evaluate_output(
            runtime,
            answer=current_answer,
            citations=citations,
            completed_tools=completed_tools if trace_complete else None,
        )
        return decision, {
            "allowed": decision.allowed,
            "decision": decision.decision,
            "violations": decision.violations,
            "suggestions": decision.suggestions,
        }

    deterministic, deterministic_dict = _deterministic_result()
    if level == "none" and deterministic.allowed:
        return {
            "verdict": "pass",
            "answer": current_answer,
            "reviewers": [],
            "evidence": [],
            "feedback": [],
            "violations": [],
            "affected_claims": [],
            "manual_review": _build_manual_review(
                required=False, violations=[], affected_claims=[], suggestions=[]
            ),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "revised": False,
            "annotated": False,
            "repair_attempts": 0,
            "attempts": 0,
            "review_rounds": [],
            "new_tools": [],
            "new_citation_count": 0,
        }
    effective_level = "checkpoint" if level == "none" else level

    # A deterministic evidence gap is repaired before the sole committee round,
    # so the committee evaluates the newly retrieved evidence instead of merely
    # rephrasing the first draft.
    if not deterministic.allowed and remediate is not None and max_repair_attempts:
        repair_attempts = 1
        try:
            repaired = await remediate(
                {
                    "attempt": 1,
                    "source": "deterministic",
                    "original_task": task,
                    "answer": current_answer,
                    "violations": deterministic.violations,
                    "suggestions": deterministic.suggestions,
                    "feedback": [],
                    "citations": list(citations),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ontology-review] tool remediation failed: %s", exc)
            all_feedback.append(f"自动补充证据失败：{str(exc)[:200]}")
            repaired = ""
        if is_substantive_revision(repaired):
            current_answer = repaired.strip()
            deterministic, deterministic_dict = _deterministic_result()
        elif repaired.strip():
            all_feedback.append("自动修订未生成完整正文，已保留原文。")

    common = {
        "task": task,
        "answer": current_answer,
        "runtime": runtime,
        "trace": trace,
        "citations": citations,
        "deterministic": deterministic_dict,
        "user_id": user_id,
        "model_name": model_name,
        "model_provider_id": model_provider_id,
    }
    if effective_level == "committee":
        committee_size = max(
            (
                int(pack.get("config", {}).get("committee_size", 3))
                for pack in runtime.get("packs", [])
            ),
            default=3,
        )
        perspectives = [
            _PERSPECTIVES[index % len(_PERSPECTIVES)] + f"（席位 {index + 1}）"
            for index in range(committee_size)
        ]
        reviewers = await asyncio.gather(
            *(_review_once(perspective, **common) for perspective in perspectives)
        )
        counts = Counter(item["verdict"] for item in reviewers)
        verdict, votes = counts.most_common(1)[0]
        if votes <= committee_size // 2:
            verdict = "escalate"
    else:
        reviewers = [await _review_once(_PERSPECTIVES[0], **common)]
        verdict = reviewers[0]["verdict"]

    if not deterministic.allowed and verdict == "pass":
        verdict = "revise"
    round_feedback = [str(item.get("feedback") or "") for item in reviewers]
    round_feedback.extend(deterministic.suggestions)
    round_evidence = [
        str(item) for reviewer in reviewers for item in reviewer.get("evidence", []) if str(item)
    ]
    round_claims = [
        claim
        for reviewer in reviewers
        for claim in reviewer.get("affected_claims", [])
        if isinstance(claim, dict)
    ]
    all_feedback.extend(round_feedback)
    all_evidence.extend(round_evidence)
    final_affected_claims = round_claims
    final_reviewers = reviewers
    review_rounds.append(
        {
            "round": 1,
            "verdict": verdict,
            "deterministic": deterministic_dict,
            "reviewers": reviewers,
        }
    )

    # A committee wording/evidence revision gets one tools-enabled continuation,
    # but is deliberately not sent through a second committee round.
    if verdict == "revise" and repair_attempts == 0 and max_repair_attempts:
        repair_attempts = 1
        try:
            if remediate is not None:
                revised = await remediate(
                    {
                        "attempt": 1,
                        "source": "committee",
                        "original_task": task,
                        "answer": current_answer,
                        "violations": deterministic.violations,
                        "suggestions": deterministic.suggestions,
                        "feedback": _dedupe_strings(round_feedback),
                        "affected_claims": round_claims,
                        "citations": list(citations),
                    }
                )
            elif deterministic.allowed:
                revised = await _revise_answer(
                    task=task,
                    answer=current_answer,
                    feedback=_dedupe_strings(round_feedback),
                    user_id=user_id,
                    model_name=model_name,
                    model_provider_id=model_provider_id,
                    runtime=runtime,
                )
            else:
                revised = ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ontology-review] committee remediation failed: %s", exc)
            all_feedback.append(f"自动修订失败：{str(exc)[:200]}")
            revised = ""
        if is_substantive_revision(revised):
            current_answer = revised.strip()
            deterministic, _ = _deterministic_result()
        elif revised.strip():
            all_feedback.append("自动修订未生成完整正文，已保留原文。")

    final_violations = list(deterministic.violations)
    final_suggestions = list(deterministic.suggestions)
    annotated = verdict == "escalate" or not deterministic.allowed
    manual_review = _build_manual_review(
        required=annotated,
        violations=final_violations,
        affected_claims=final_affected_claims,
        suggestions=final_suggestions or all_feedback,
    )
    new_tools = _dedupe_strings(
        [
            str(item.get("tool_name") or "")
            for item in trace[initial_trace_len:]
            if item.get("type") == "tool_result"
        ]
    )
    evidence = _dedupe_strings(all_evidence)
    feedback = _dedupe_strings(all_feedback + final_suggestions)
    latency_ms = round((time.monotonic() - started) * 1000)
    try:
        from core.services.ontology_service import record_enforcement_event, record_review_run

        review_payload = {
            "user_id": user_id or None,
            "chat_id": chat_id,
            "pack_version_ids": runtime.get("version_ids", []),
            "level": "checkpoint" if level == "none" else level,
            "subject_type": "final_answer",
            "verdict": verdict,
            "evidence": evidence,
            "feedback": "\n".join(feedback),
            "reviewers": [
                {**reviewer, "round": round_info["round"]}
                for round_info in review_rounds
                for reviewer in round_info["reviewers"]
            ],
            "latency_ms": latency_ms,
        }
        audit_tasks = [asyncio.to_thread(record_review_run, review_payload)]
        for pack in runtime.get("packs", []):
            audit_tasks.append(
                asyncio.to_thread(
                    record_enforcement_event,
                    {
                        "user_id": user_id or None,
                        "chat_id": chat_id,
                        "pack_id": pack.get("pack_id"),
                        "version_id": pack.get("version_id"),
                        "rule_id": (
                            deterministic.violations[0].get("rule_id")
                            if deterministic.violations
                            else f"review:{level}"
                        ),
                        "stage": "output",
                        "event_type": "output_review",
                        "decision": verdict,
                        "mode": "enforce",
                        "target": "final_answer",
                        "latency_ms": latency_ms,
                        "details": {
                            "governance_run_id": runtime.get("governance_run_id"),
                            "review_owner": (runtime.get("output_review") or {}).get("owner"),
                            "violations": deterministic.violations,
                            "evidence": evidence,
                            "feedback": review_payload["feedback"],
                            "repair_attempts": repair_attempts,
                            "review_rounds": len(review_rounds),
                        },
                    },
                )
            )
        await asyncio.gather(*audit_tasks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ontology-review] audit persistence failed: %s", exc)
    if verdict in {"revise", "escalate"} and chat_id:
        try:
            from core.services.ontology_evolution_service import schedule_ontology_evolution

            schedule_ontology_evolution(user_id=user_id or "system")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ontology-review] evolution scheduling failed: %s", exc)
    return {
        "verdict": verdict,
        "answer": current_answer,
        "reviewers": final_reviewers,
        "evidence": evidence,
        "feedback": feedback,
        "violations": final_violations,
        "affected_claims": final_affected_claims,
        "manual_review": manual_review,
        "latency_ms": latency_ms,
        "revised": current_answer != original_answer,
        "annotated": annotated,
        "repair_attempts": repair_attempts,
        "attempts": max(1, len(review_rounds)),
        "review_rounds": review_rounds,
        "new_tools": new_tools,
        "new_citation_count": max(0, len(citations) - initial_citation_len),
    }
