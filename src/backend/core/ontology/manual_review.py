"""Build the payload for human ontology review cards."""
from __future__ import annotations
from typing import Any

def dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def build_manual_review(
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
        "actions": [item[:500] for item in dedupe_strings(suggestions)[:6]],
    }
