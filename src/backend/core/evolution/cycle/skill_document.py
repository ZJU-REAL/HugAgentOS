"""Render distilled skills as SKILL.md documents."""
from __future__ import annotations
from typing import Any, Dict, List, Sequence
import logging
logger = logging.getLogger(__name__)

def skill_label(tools: Sequence[str], *, rules_only: bool = False) -> str:
    """A human- and model-readable skill name.

    The name and description are the *only* things the model sees before it
    decides whether to open a skill, so they have to describe the job. The
    candidate's ``hypothesis`` describes the evidence ("「…」类请求在 N 次独立
    会话中以同一序列成功完成") — true, and useless for that decision, since it
    says nothing about when the skill applies. It belongs in the provenance
    section, not the title.
    """
    head = " → ".join(tools[:4]) + ("…" if len(tools) > 4 else "")
    return ("工具顺序规则：" if rules_only else "固定调用顺序：") + head


def skill_description(
    tools: Sequence[str], constraints: Sequence[Dict[str, Any]], *, rules_only: bool
) -> str:
    """One line, single-line by necessity — the frontmatter parser is line-based."""
    joined = "、".join(f"`{t}`" for t in tools[:6])
    if rules_only:
        return (
            f"当任务用到 {joined} 时，按已验证的先后顺序约束安排调用次序"
            f"（{len(constraints)} 条约束，来自历史成功执行）。"
        )
    return (
        f"当任务需要 {joined} 且执行顺序会影响结果时使用；"
        f"按 {' → '.join(tools)} 的顺序调用。"
    )


def rules_consistent_with(
    constraints: Sequence[Dict[str, Any]], tools: Sequence[str]
) -> List[Dict[str, Any]]:
    """Drop rules whose direction the skill's own step list violates.

    The loop attaches every rule touching a skill's tools, and scoping makes that
    formally sound — a rule valid in ``finance`` and a sequence distilled from
    ``trap`` genuinely can point opposite ways. But the rendered document then
    tells the model to call A before B in its steps and B before A two sections
    later, with nothing saying which wins. Observed on a real materialisation.

    For this skill's own tool set the step list is the governing statement, so a
    rule contradicting it is describing some other context and does not belong in
    this document. It remains attached to the skills whose sequences agree with
    it, and in ``ordering_constraints.json`` for any consumer that reasons about
    scope itself.
    """
    if not tools:
        return list(constraints)
    position = {tool: index for index, tool in enumerate(tools)}
    kept: List[Dict[str, Any]] = []
    for rule in constraints:
        before, after = rule.get("before"), rule.get("after")
        if before in position and after in position and position[before] > position[after]:
            logger.info(
                "[activation] rule %s<%s omitted: contradicts this skill's own order",
                before,
                after,
            )
            continue
        kept.append(rule)
    return kept


def ordering_rules_section(constraints: Sequence[Dict[str, Any]]) -> str:
    """Render the learned ordering constraints into the body the model reads.

    Without this the transferable half of what was learned never reaches the
    runtime. ``ordering_constraints.json`` is written alongside, but nothing in
    the agent path reads extra files as policy — the model reads SKILL.md, so a
    rule that is not written here is a rule the model never receives.

    Each rule carries its own scope. Stating where it was contradicted matters
    as much as stating where it held: a rule applied in the context that
    disproved it actively breaks that context.
    """
    shippable = [r for r in constraints if not (r.get("contradicted_in") or [])]
    withheld = [r for r in constraints if (r.get("contradicted_in") or [])]

    lines: List[str] = []
    if shippable:
        lines += ["", "## 顺序约束（可迁移）", ""]
        lines.append("下列约束在历史成功执行中无反例，适用于本技能覆盖的工具：")
        lines.append("")
        for rule in shippable:
            before, after = rule.get("before"), rule.get("after")
            validated = [str(x) for x in (rule.get("validated_in") or [])]
            line = f"- 先 `{before}` 再 `{after}`"
            if validated:
                line += f"——已在 {'、'.join(validated)} 类任务中验证"
            line += f"（证据 {rule.get('support', 0)} 条）"
            lines.append(line)

    if withheld:
        # Scoped rules are deliberately kept out of the model's view.
        #
        # Measured twice against a real model, and both framings failed:
        #
        # * exception as a trailing qualifier ("在 X 类任务中不适用") — the model
        #   applied the rule in X anyway, 5 of 5 times;
        # * exception first and imperative ("若属于 X 则整条作废，那里顺序相反")
        #   — the model then applied the *reversed* order everywhere, dropping the
        #   family the rule was valid in from 50% to 17% and disturbing an
        #   unrelated control family.
        #
        # So a text annotation cannot make a model route on task type: it either
        # ignores the exception or over-applies it. Handing it a rule that is
        # wrong somewhere makes the system worse where the rule is wrong, which
        # is a poor trade for a benefit available only where it is right.
        #
        # The rule is not discarded — it stays in ``ordering_constraints.json``
        # for a runtime applier that knows the task type and can decide before
        # injection, which is where a scope decision belongs. Until that exists,
        # withholding is the conservative and measurably better option.
        lines += ["", "## 已扣留的上下文相关约束", ""]
        lines.append(
            f"另有 {len(withheld)} 条约束在部分任务类型中被证据推翻，"
            "因此**不在本技能中给出**——它们只在特定任务类型下成立，"
            "由运行时按任务类型判定后注入，不交由模型自行取舍。"
        )
        for rule in withheld:
            lines.append(
                f"- （已扣留）`{rule.get('before')}` / `{rule.get('after')}`："
                f"在 {'、'.join(str(x) for x in (rule.get('validated_in') or []))} 成立，"
                f"在 {'、'.join(str(x) for x in (rule.get('contradicted_in') or []))} 被推翻"
            )
    return "\n".join(lines)


def skill_markdown(
    *,
    skill_id: str,
    label: str,
    description: str,
    tools: Sequence[str],
    evidence: int,
    constraints: Sequence[Dict[str, Any]] = (),
    rationale: str = "",
    rules_only: bool = False,
) -> str:
    """Render the SKILL.md body for a distilled skill.

    ``name`` is the skill **id** — the loader treats that field as the id and
    rejects anything outside ``[a-z0-9_-]``. Writing the human label there made
    every materialised skill unparseable: it still showed up in the capability
    list (that metadata comes from table columns) while ``load_skill_full``
    returned ``None``, so the skill was present and inert.

    States its provenance in the document itself, so anyone reading the skill —
    not just anyone reading the console — knows where it came from and on how
    much evidence it rests.
    """
    # Applicability is declared for the **whole skill**, not per rule. Measured:
    # withholding a contradicted rule from the text was not enough, because the
    # step list generalises on its own — the model applied this skill's order to
    # a family where the reverse is correct, 5 of 5 times, with the rule absent.
    # So the exclusion has to bind the steps too, which means stating it once, at
    # the top, before anything the model could copy.
    excluded = sorted(
        {
            str(name)
            for rule in constraints
            for name in (rule.get("contradicted_in") or [])
            if str(name)
        }
    )
    covered = sorted(
        {
            str(name)
            for rule in constraints
            for name in (rule.get("validated_in") or [])
            if str(name) and str(name) not in excluded
        }
    )
    scope_block = ""
    if excluded:
        scope_block = (
            "## 不适用范围（先读这一段）\n\n"
            f"**本技能不适用于以下任务类型：{'、'.join(excluded)}。**"
            "在这些场景中，本技能给出的顺序已被历史证据推翻——"
            "遇到它们请完全忽略本技能，按任务本身的依赖关系自行规划。\n\n"
            + (
                f"本技能仅适用于：{'、'.join(covered)}。\n\n"
                if covered
                else ""
            )
        )

    if rules_only:
        body_head = (
            scope_block
            + "## 适用条件\n\n"
            "当任务用到下列工具、且它们的先后顺序会影响结果时，按「顺序约束」小节排序。\n\n"
            "## 涉及工具\n\n"
            + "\n".join(f"- `{tool}`" for tool in tools)
        )
    else:
        body_head = (
            scope_block
            + "## 适用条件\n\n"
            "当任务需要按固定顺序完成下列工具调用时使用本技能。\n\n"
            "## 步骤\n\n"
            + "\n".join(f"{i}. `{tool}`" for i, tool in enumerate(tools, start=1))
        )

    # A rules-only skill has no governing step list, so nothing there can be
    # contradicted; for a sequence skill the steps win and conflicting rules are
    # about other contexts.
    shown_rules = constraints if rules_only else rules_consistent_with(constraints, tools)

    provenance = (
        f"本技能由能力进化流水线从 {evidence} 条历史执行证据中沉淀而成，非人工撰写。"
    )
    if rationale:
        provenance += f"\n\n沉淀依据：{rationale}"

    return f"""---
name: {skill_id}
display_name: {label}
description: {description}
---

# {label}

{description}

{body_head}
{ordering_rules_section(shown_rules)}

## 来源

{provenance}
"""


def body_of(rendered: str) -> str:
    """The body of a rendered SKILL.md, without frontmatter or provenance.

    Re-rendering from the body rather than storing the generator's output
    verbatim is what lets the id change between drafting and activation (an
    evolution prefix, an owner suffix) without the document's ``name`` field
    disagreeing with the row it is stored under — a mismatch that makes the
    skill unloadable while still listing it as available.
    """
    text = rendered or ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    # Drop the rendered title/description pair and the provenance section; both
    # are regenerated, and keeping them would double on every re-render.
    marker = text.find("## ")
    if marker != -1:
        text = text[marker:]
    source = text.find("\n## 来源")
    if source != -1:
        text = text[:source]
    return text.strip()


def tools_from_constraints(constraints: Sequence[Dict[str, Any]]) -> List[str]:
    """The tools a set of ordering rules talks about, in an order they permit.

    Not a claimed execution plan — a rules-only skill has no fixed sequence. It
    is the tool list the skill declares, listed in an order that does not
    contradict its own rules so the document does not read as self-refuting.
    """
    tools: List[str] = []
    for rule in constraints:
        for key in ("before", "after"):
            name = str(rule.get(key) or "")
            if name and name not in tools:
                tools.append(name)
    if not tools:
        return []
    from core.evolution.cycle.promotion_types import OrderingConstraint
    from core.evolution.cycle.ordering import apply_ordering_constraints

    parsed = [
        OrderingConstraint(
            before=str(rule.get("before") or ""),
            after=str(rule.get("after") or ""),
            support=int(rule.get("support") or 0),
            contexts=int(rule.get("contexts") or 0),
            validated_in=tuple(rule.get("validated_in") or ()),
            # Scope is deliberately dropped for this one purpose: we are only
            # ordering the *document's* tool list, not deciding applicability.
        )
        for rule in constraints
        if rule.get("before") and rule.get("after")
    ]
    return apply_ordering_constraints(tools, parsed)
