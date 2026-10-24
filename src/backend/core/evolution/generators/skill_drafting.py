"""Prompt, parse and validate a model-drafted skill body."""
from __future__ import annotations
import re
from typing import Any, List, Sequence, Tuple
from core.evolution.generators import skill_evidence
from core.evolution.generators import skill_gen_constants

def format_list(items: Sequence[Any], empty: str = "（无）") -> str:
    if not items:
        return empty
    lines = []
    for item in items:
        if isinstance(item, (list, tuple)):
            lines.append("- " + " → ".join(str(x) for x in item))
        elif isinstance(item, dict):
            lines.append("- " + "；".join(f"{k}: {v}" for k, v in item.items() if v))
        else:
            lines.append(f"- {item}")
    return "\n".join(lines)


def build_prompt(pack: skill_evidence.EvidencePack, violations: Sequence[str]) -> str:
    exclusion_hint = (
        f"证据显示本技能在以下任务类型中被推翻，必须写进不适用范围：{'、'.join(pack.excluded_task_types)}。"
        if pack.excluded_task_types
        else "如果证据没有显示明确的排除场景，就写明本技能只适用于「适用条件」描述的场景，其余场景请忽略本技能。"
    )
    retry_hint = ""
    if violations:
        retry_hint = (
            "# 上一版被打回的原因（必须修正）\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n"
        )
    return skill_gen_constants.DRAFT_PROMPT.format(
        objective_count=len(pack.objectives),
        objectives=format_list(pack.objectives[:10]),
        tools=format_list([f"`{t}`" for t in pack.tools]),
        sequences=format_list(pack.successful_sequences[:6]),
        failures=format_list(pack.failures[:6]),
        constraints=format_list(
            [
                f"先 {c.get('before')} 再 {c.get('after')}"
                + (
                    f"（在 {'、'.join(str(x) for x in (c.get('validated_in') or []))} 中验证）"
                    if c.get("validated_in")
                    else ""
                )
                for c in pack.ordering_constraints
                if not (c.get("contradicted_in") or [])
            ]
        ),
        procedures=format_list(
            [
                p.get("rule", "") + (f"（原因：{p['why']}）" if p.get("why") else "")
                for p in pack.procedures[:10]
            ]
        ),
        exclusion_hint=exclusion_hint,
        max_chars=skill_gen_constants.MAX_BODY_CHARS,
        retry_hint=retry_hint,
    )


def parse_draft(raw: str) -> Tuple[str, str, str]:
    """``(display_name, description, body)`` from the model's reply."""
    display_name = ""
    description = ""
    body = ""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    body_marker = re.search(r"^BODY:\s*$", text, re.MULTILINE)
    head = text[: body_marker.start()] if body_marker else text
    if body_marker:
        body = text[body_marker.end() :].strip()

    for line in head.splitlines():
        if line.upper().startswith("DISPLAY_NAME:"):
            display_name = line.split(":", 1)[1].strip()
        elif line.upper().startswith("DESCRIPTION:"):
            description = line.split(":", 1)[1].strip()
    return display_name, description, body


def validate_draft(
    *,
    skill_id: str,
    display_name: str,
    description: str,
    body: str,
    allowed_tools: Sequence[str],
) -> List[str]:
    """Every constraint stage B was told about, checked. Returns violations."""
    violations: List[str] = []

    if not body.strip():
        return [skill_gen_constants.REJECT_EMPTY]
    if not display_name.strip() or not description.strip():
        violations.append(skill_gen_constants.REJECT_NO_FRONTMATTER)
    if not skill_gen_constants.FRONTMATTER_NAME.match(skill_id or ""):
        # The loader treats ``name`` as the id and rejects anything outside
        # ``[a-z0-9_-]``. A skill that fails this still appears in the capability
        # list — that metadata comes from table columns — while loading returns
        # nothing, so it is present and inert, which is the hardest failure of
        # all to notice.
        violations.append(skill_gen_constants.REJECT_BAD_NAME)

    if skill_gen_constants.CODE_FENCE.search(body):
        violations.append(skill_gen_constants.REJECT_CODE_BLOCK)
    if skill_gen_constants.URL.search(body):
        violations.append(skill_gen_constants.REJECT_EXTERNAL_URL)
    if len(body) > skill_gen_constants.MAX_BODY_CHARS:
        violations.append(skill_gen_constants.REJECT_TOO_LONG)
    if len(body) < skill_gen_constants.MIN_BODY_CHARS:
        violations.append(skill_gen_constants.REJECT_TOO_SHORT)

    if "## 不适用范围" not in body:
        violations.append(skill_gen_constants.REJECT_NO_SCOPE)
    else:
        # The exclusion has to precede anything the model could copy: measured,
        # withholding a contradicted rule was not enough, because the step list
        # generalises on its own.
        scope_at = body.index("## 不适用范围")
        earlier_section = re.search(r"^## ", body[:scope_at], re.MULTILINE)
        if earlier_section:
            violations.append(skill_gen_constants.REJECT_NO_SCOPE)
    if "## 常见失败与处理" not in body and "## 步骤" not in body:
        violations.append(skill_gen_constants.REJECT_NO_CHECKPOINTS)

    allowed = {str(t) for t in allowed_tools}
    mentioned = {m for m in skill_gen_constants.TOOL_MENTION.findall(body)}
    # Only identifiers that look like tool names are checked: prose in backticks
    # (a field name, a value) must not be mistaken for a tool grant, and a real
    # tool name always appears verbatim in the evidence.
    unknown = sorted(
        name
        for name in mentioned
        if name not in allowed and "_" in name and name.islower()
    )
    if unknown:
        violations.append(f"{skill_gen_constants.REJECT_UNKNOWN_TOOL}: {unknown}")

    return violations


def render_skill_markdown(
    *,
    skill_id: str,
    display_name: str,
    description: str,
    body: str,
    support: int,
    rationale: str = "",
) -> str:
    """Wrap a validated body in the frontmatter the loader requires.

    ``name`` is the skill **id**, not the label — writing the human label there
    made every materialised skill unparseable.
    """
    provenance = (
        f"本技能由能力进化流水线从 {support} 条历史执行证据中沉淀而成，经机器校验后由人工批准。"
    )
    if rationale:
        provenance += f"\n\n沉淀依据：{rationale}"
    return f"""---
name: {skill_id}
display_name: {display_name}
description: {description}
---

# {display_name}

{description}

{body.strip()}

## 来源

{provenance}
"""
