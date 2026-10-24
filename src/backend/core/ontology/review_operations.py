"""Run one ontology review and revise the answer when it fails."""
from __future__ import annotations
from typing import Any
from core.harness.registry.capability_runtime import child_scope
from core.harness.registry.model_services import run_text_agent as _run_text_agent
from core.infra.json_output import parse_json_lenient as _parse_json_lenient
from core.infra.logging import get_logger
from core.ontology import review_prompt
from core.ontology import reviewer_state

logger = get_logger(__name__)

async def review_once(
    perspective: str,
    **kwargs,
) -> dict[str, Any]:
    prompt = review_prompt.review_prompt(
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
            capability_scope=child_scope(
                kwargs.get("capability_scope", ""), "ontology-review", perspective
            ),
        )
        obj = _parse_json_lenient(text)
        if not isinstance(obj, dict) or obj.get("verdict") not in reviewer_state.VALID_VERDICTS:
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


async def revise_answer(
    *,
    task: str,
    answer: str,
    feedback: list[str],
    user_id: str,
    model_name: str | None,
    model_provider_id: str | None,
    runtime: dict[str, Any],
    capability_scope: str = "",
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
            capability_scope=child_scope(capability_scope, "ontology-model", "revise"),
        )
    ).strip()
