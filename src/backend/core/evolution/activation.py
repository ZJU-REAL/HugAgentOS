"""Materialise an approved candidate into a runtime capability.

This is the step the loop was missing.  Everything upstream — discovery,
attribution, promotion, the IR, replay — produced a *candidate*, and a candidate
is a proposal.  Nothing turned it into an asset the agent could actually load,
so measured end to end the system learned nothing: the benchmark's "after" arm
saw exactly zero new skills.

Materialisation closes that gap, and it is deliberately the **only** way an
evolution artefact reaches the runtime.  It sits behind the release controller
so every governance property upstream still holds:

* the candidate must have cleared the verification its risk tier demands;
* the approver must not be the proposer;
* the previous state is captured **before** the write, so rollback has somewhere
  to go rather than having to reconstruct one afterwards.

Materialised skills are tagged so they can always be told apart from
hand-authored ones. A capability the system wrote about itself should never be
indistinguishable from one a person wrote.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.db.engine import SessionLocal

logger = logging.getLogger(__name__)

# Marks a skill that the evolution loop wrote. Never remove this on update: it
# is what lets an operator see at a glance which capabilities were authored by
# the system rather than by a person.
EVOLUTION_TAG = "evolved"
EVOLUTION_PREFIX = "evo-"

# Operator stop switch, in ``system_configs`` so an incident does not need a
# redeploy to halt every layer.
FREEZE_CONFIG_KEY = "evolution_frozen"


class ActivationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _skill_label(tools: Sequence[str], *, rules_only: bool = False) -> str:
    """A human- and model-readable skill name.

    The name and description are the *only* things the model sees before it
    decides whether to open a skill, so they have to describe the job. The
    candidate's ``hypothesis`` reads "5 个 Episode 出现相同工具子序列且成功率
    100%" — true, and useless for that decision, since it says nothing about
    when the skill applies. It belongs in the provenance section, not the title.
    """
    head = " → ".join(tools[:4]) + ("…" if len(tools) > 4 else "")
    return ("工具顺序规则：" if rules_only else "固定调用顺序：") + head


def _skill_description(
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


def _rules_consistent_with(
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


def _ordering_rules_section(constraints: Sequence[Dict[str, Any]]) -> str:
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


def _skill_markdown(
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
    shown_rules = constraints if rules_only else _rules_consistent_with(constraints, tools)

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
{_ordering_rules_section(shown_rules)}

## 来源

{provenance}
"""


def _body_of(rendered: str) -> str:
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


def _capture_previous(db, skill_id: str) -> Optional[Dict[str, Any]]:
    """Snapshot the current state so rollback has a concrete target."""
    from core.db.models import AdminSkill

    row = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
    if row is None:
        return None
    return {
        "skill_id": row.skill_id,
        "skill_content": row.skill_content,
        "display_name": row.display_name,
        "description": row.description,
        "version": row.version,
        "tags": list(row.tags or []),
        "allowed_tools": list(row.allowed_tools or []),
        "extra_files": dict(row.extra_files or {}),
        "is_enabled": bool(row.is_enabled),
    }


def _is_frozen(db) -> bool:
    """Global stop switch, readable without a redeploy.

    An incident needs a way to stop every layer activating *now*, so it lives in
    ``system_configs`` rather than in an environment variable.
    """
    try:
        from core.db.models import SystemConfig

        row = (
            db.query(SystemConfig.config_value)
            .filter(SystemConfig.config_key == FREEZE_CONFIG_KEY)
            .first()
        )
        return bool(row) and str(row[0]).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 - absence of the row means "not frozen"
        return False


def _skill_layer_state(db) -> Tuple[Optional[datetime], int]:
    """When a skill last went live, and how many releases are mid-flight.

    Read from the release ledger rather than kept in memory: a per-process
    limiter resets on every restart and multiplies by the number of replicas,
    which is exactly how a cooldown quietly stops existing.
    """
    from core.db.models.evolution import EvolutionRelease
    from core.evolution.release import ACTIVE, IN_FLIGHT_STAGES

    last_row = (
        db.query(EvolutionRelease.created_at)
        .filter(
            EvolutionRelease.target_kind == "skill",
            EvolutionRelease.stage == ACTIVE,
        )
        .order_by(EvolutionRelease.created_at.desc())
        .first()
    )
    in_flight = (
        db.query(EvolutionRelease)
        .filter(EvolutionRelease.stage.in_(list(IN_FLIGHT_STAGES)))
        .count()
    )
    last = last_row[0] if last_row else None
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last, int(in_flight or 0)


def _check_pace(db, *, tenant_id: str, layer: str = "skill") -> None:
    """Enforce the multi-timescale gate the design declares.

    Skills sit on an hours-to-days cadence so the system does not become
    non-stationary. That was a documented number with no enforcement: nothing
    consulted ``timescales`` before writing a live asset, so ten activations
    could land in one minute and no metric movement afterwards would be
    attributable to any of them.
    """
    from core.evolution.timescales import RateLimiter

    last, in_flight = _skill_layer_state(db)
    limiter = RateLimiter(frozen=_is_frozen(db))
    if last is not None:
        limiter.last_activation[layer] = last
    allowed, reason = limiter.may_activate(
        layer, tenant=tenant_id or "default", concurrent_activations=in_flight
    )
    if not allowed:
        raise ActivationError("pace_limited", f"节奏闸门拒绝本次激活：{reason}")


def _entry_stage(
    candidate,
    *,
    approver: str,
    force: bool,
    owner_user_id: Optional[str],
) -> Tuple[str, int]:
    """The stage this candidate has actually earned, and its traffic share.

    Previously every activation wrote ``active`` at 100%, whatever the risk tier
    said. ``ir.MIN_VERIFICATION`` declares that a high-risk change owes replay,
    shadow, canary *and* human approval before going live, and a brand-new
    skill is exactly that tier — so the old behaviour skipped two of the four
    stages its own IR demanded.

    A personal activation is the one case that legitimately goes straight live:
    its blast radius is one consenting user, and the ramp exists to bound blast
    radius. It is recorded as scoped rather than as full traffic.
    """
    from core.evolution.ir import RISK_LOW, required_verification
    from core.evolution.release import ACTIVE, REPLAY_PASSED, SHADOW

    risk = str(candidate.risk_tier or "").strip() or RISK_LOW
    replayed = candidate.status in (REPLAY_PASSED, ACTIVE)

    if not replayed:
        # ``force`` waives the replay *stage* — and only for the tier whose
        # verification requirement is replay alone. Waiving it for a tier that
        # also owes shadow and canary is not a shortcut, it is the whole gate.
        if not force:
            raise ActivationError(
                "verification_incomplete",
                f"候选尚未通过回放（当前 {candidate.status}）",
            )
        if risk != RISK_LOW:
            raise ActivationError(
                "force_forbidden_for_risk",
                f"force 只允许用于 low 风险候选，本候选为 {risk}；"
                f"需完成 {list(required_verification(risk))}",
            )

    if owner_user_id:
        return ACTIVE, 0

    if set(required_verification(risk)) <= {"replay"}:
        return ACTIVE, 100

    # Higher tiers enter at shadow: the asset exists and is measurable, and
    # reaches nobody until an operator ramps it.
    return SHADOW, 0


def materialise_skill_candidate(
    candidate_id: str,
    *,
    approver: str,
    force: bool = False,
    owner_user_id: Optional[str] = None,
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """Materialise an approved skill candidate, at the stage it has earned.

    This is the only path by which an evolution artefact reaches the agent, and
    it now enters the release pipeline rather than bypassing it:

    * ``low`` risk (verification = replay only) goes live at 100%;
    * ``medium`` / ``high`` enter **shadow**, reaching nobody, and must be ramped
      through canary by :func:`advance_skill_release`;
    * a personal activation (``owner_user_id``) goes live scoped to that user.

    ``force`` waives the replay *stage* and only for ``low`` risk. It never
    waives separation of duties — an override is exactly what an attacker or an
    over-eager script would reach for.

    ``owner_user_id`` scopes the resulting skill to one person. A personal
    activation cannot change what anyone else's agent does, which is what lets a
    user accept a capability distilled from their own conversations without an
    administrator standing in the way.
    """
    from core.db.models import AdminSkill
    from core.db.models.evolution import EvolutionCandidate, EvolutionRelease
    from core.evolution.release import (
        ACTIVE,
        IN_FLIGHT_STAGES,
        REPLAY_PASSED,
        ROLLBACK_MANUAL,
        new_release_id,
    )

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if candidate.target_kind != "skill":
            raise ActivationError(
                "unsupported_kind", f"暂不支持物化 {candidate.target_kind} 候选"
            )

        # Separation of duties. No force path — see the docstring.
        if approver and approver == (candidate.proposer or ""):
            raise ActivationError(
                "self_approval_forbidden", "生成者不能激活自己提出的候选"
            )
        if not approver:
            raise ActivationError("approver_required", "激活必须署名")

        stage, traffic = _entry_stage(
            candidate, approver=approver, force=force, owner_user_id=owner_user_id
        )
        # Pace and freeze apply to anything that becomes live immediately. A
        # shadow entry changes nothing users can see, so holding it back would
        # only delay measurement.
        if stage == ACTIVE and not owner_user_id:
            _check_pace(db, tenant_id=tenant_id)

        ir = candidate.ir or {}
        changes = ir.get("changes") or []
        tools: List[str] = []
        constraints: List[Dict[str, Any]] = []
        document: Optional[Dict[str, Any]] = None
        # ref → owning user. Demotion happens inside one person's store, so a
        # skill compiled from several people's memories needs the owner of each.
        source_memories: Dict[str, str] = {}
        for change in changes:
            sequence = change.get("tool_sequence")
            if isinstance(sequence, list) and sequence and not tools:
                tools = [str(t) for t in sequence]
            rules = change.get("ordering_constraints")
            if isinstance(rules, list) and rules:
                constraints = rules
            written = change.get("document")
            if isinstance(written, dict) and written.get("content"):
                document = written
            for item in change.get("source_memories") or []:
                if isinstance(item, dict) and item.get("ref"):
                    source_memories[str(item["ref"])] = str(item.get("user_id") or "")

        skill_id = candidate.target_asset_id
        if not skill_id.startswith(EVOLUTION_PREFIX):
            skill_id = EVOLUTION_PREFIX + skill_id.replace("auto-", "")
        if owner_user_id:
            # Distinct id per owner: a personal activation must never overwrite
            # the global asset, nor another user's copy of the same candidate.
            skill_id = f"{skill_id}-u{owner_user_id[-8:]}"

        if document is not None:
            # A written skill: the generator produced the body and the machine
            # validator already refused everything it was told to refuse. It is
            # re-checked here against this candidate's id, because the id gains
            # a prefix (and, for a personal activation, an owner suffix) after
            # the draft was validated — and an id the loader rejects yields a
            # skill that shows up in the capability list and loads as nothing.
            from core.evolution.skill_gen import render_skill_markdown, validate_draft

            tools = [str(t) for t in (document.get("allowed_tools") or [])]
            display_name = str(document.get("display_name") or "")
            description = str(document.get("description") or "")
            body = _body_of(str(document.get("content") or ""))
            violations = validate_draft(
                skill_id=skill_id,
                display_name=display_name,
                description=description,
                body=body,
                allowed_tools=tools,
            )
            if violations:
                raise ActivationError(
                    "draft_failed_validation",
                    f"技能正文未通过机器校验，拒绝物化: {violations}",
                )
            rules_only = False
            content = render_skill_markdown(
                skill_id=skill_id,
                display_name=display_name,
                description=description,
                body=body,
                support=len(candidate.evidence_refs or []),
                rationale=candidate.hypothesis or "",
            )
        else:
            # A candidate carrying only ordering rules is still a capability,
            # and on real traffic it is the *common* case: five runs repeating
            # one identical tool sequence is rare, while the same pairwise
            # ordering recurring across different tool sets is not.
            rules_only = not tools and bool(constraints)
            if rules_only:
                tools = _tools_from_constraints(constraints)
            if not tools:
                raise ActivationError(
                    "no_tool_sequence", "候选既未声明工具顺序、也未携带顺序约束，无法物化"
                )
            display_name = _skill_label(tools, rules_only=rules_only)
            description = _skill_description(tools, constraints, rules_only=rules_only)
            content = _skill_markdown(
                skill_id=skill_id,
                label=display_name,
                description=description,
                tools=tools,
                evidence=len(candidate.evidence_refs or []),
                constraints=constraints,
                rationale=candidate.hypothesis or "",
                rules_only=rules_only,
            )

        previous = _capture_previous(db, skill_id)

        # One release in flight per asset. Two concurrent ramps on the same skill
        # make any measured effect unattributable and leave rollback without a
        # single target.
        conflicting = (
            db.query(EvolutionRelease)
            .filter(
                EvolutionRelease.target_asset_id == skill_id,
                EvolutionRelease.stage.in_(list(IN_FLIGHT_STAGES)),
            )
            .first()
        )
        if conflicting is not None:
            raise ActivationError(
                "release_in_flight",
                f"该资产已有进行中的发布 {conflicting.release_id}（{conflicting.stage}）",
            )

        row = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
        if row is None:
            row = AdminSkill(skill_id=skill_id, created_by=f"evolution:{approver}")
            db.add(row)
        row.owner_user_id = owner_user_id or None

        row.skill_content = content
        row.display_name = display_name
        row.description = description
        row.version = _next_version(previous)
        row.tags = sorted(set((previous or {}).get("tags", [])) | {EVOLUTION_TAG})
        # The distilled ordering, in the shape the runtime reads.
        row.extra_files = {
            **(previous or {}).get("extra_files", {}),
            "tool_sequence.json": _json(tools),
            # The transferable part. A consumer that only understands sequences
            # still works; one that understands rules gets the wider benefit.
            "ordering_constraints.json": _json(constraints),
        }
        row.allowed_tools = tools
        row.is_enabled = True

        # The scope the IR asked for, now actually enforced at load time by
        # core.evolution.exposure. Recording it here rather than deriving it
        # later means a scope decision is auditable against the release it
        # governs.
        scope_filter: Dict[str, Any] = {"level": (ir.get("scope") or {}).get("level") or "workspace"}
        if owner_user_id:
            scope_filter["owner_user_id"] = owner_user_id
        else:
            scope_filter["tenant_id"] = tenant_id or "default"

        release = EvolutionRelease(
            release_id=new_release_id(),
            candidate_id=candidate_id,
            target_kind="skill",
            target_asset_id=skill_id,
            stage=stage,
            # A personal activation reaches exactly one user, so recording it as
            # full traffic would misreport the blast radius on the release board.
            traffic_percent=traffic,
            scope_filter=scope_filter,
            # Written now, not derived later: rollback needs a target that
            # existed before anything changed.
            rollback_version_id=(previous or {}).get("version") or "",
            guardrails={
                "previous": previous,
                # Which verification stages are behind us. Read by
                # advance_skill_release before every hop.
                "completed_verification": (
                    ["replay"] if candidate.status in (REPLAY_PASSED, ACTIVE) else []
                ),
                "proposer": candidate.proposer or "",
            },
            approved_by=approver,
        )
        db.add(release)

        # A personal activation leaves the candidate available for others to
        # accept, and for an administrator to consider globally. Marking it
        # ACTIVE would end its life for everyone on one person's decision.
        # A shadow entry is likewise not the end of the candidate's life.
        if not owner_user_id and stage == ACTIVE:
            candidate.status = ACTIVE
        candidate.approved_by = approver
        candidate.approved_at = datetime.now(timezone.utc)
        db.commit()

        release_id = release.release_id
        new_version = row.version
        candidate_owner = str((ir.get("scope") or {}).get("user_id") or "")
        logger.info(
            "[activation] materialised %s from %s at %s/%d%% (tools=%s written=%s)",
            skill_id,
            candidate_id,
            stage,
            traffic,
            tools,
            document is not None,
        )

    # Close the promotion chain: the memories this skill was compiled from now
    # have a cheaper carrier, so they stop consuming the retrieval budget that
    # made compiling them worthwhile. Outside the transaction above because it
    # writes to its own ledger, and down-weighted rather than removed, so
    # rolling the skill back restores them exactly.
    demoted: List[str] = []
    if source_memories:
        from core.evolution.memory_apply import demote_promoted_memories

        by_owner: Dict[str, List[str]] = {}
        for ref, owner in source_memories.items():
            # ``owner_user_id`` for a personal activation, the ref's recorded
            # owner otherwise. A ref with neither is skipped rather than guessed
            # at: applying a change to the wrong person's memories is worse than
            # not applying it.
            resolved = owner_user_id or owner or candidate_owner
            if resolved:
                by_owner.setdefault(resolved, []).append(ref)

        for owner, refs in by_owner.items():
            outcome = demote_promoted_memories(
                candidate_id=candidate_id,
                user_id=owner,
                memory_refs=refs,
                skill_id=skill_id,
            )
            demoted.extend(op["memory_ref"] for op in outcome.get("applied", []))

    return {
        "skill_id": skill_id,
        "release_id": release_id,
        "stage": stage,
        "traffic_percent": traffic,
        "scope": scope_filter,
        "demoted_memories": demoted,
        # Spelled out because "activated" previously implied "live for
        # everyone", and for anything above low risk it no longer does.
        "exposed_to": (
            "all users in scope"
            if stage == ACTIVE and not owner_user_id
            else (f"only {owner_user_id}" if owner_user_id else "nobody (shadow)")
        ),
        "tools": tools,
        "version": new_version,
        "replaced_previous": previous is not None,
    }


def advance_skill_release(
    release_id: str,
    *,
    actor: str,
    observed_metrics: Optional[Dict[str, float]] = None,
    guardrail_thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Ramp a release one step: shadow → canary 5 → 25 → 100 → active.

    One step per call, deliberately. A single "go live" button that walked the
    whole ladder would reproduce the behaviour this replaces — the point of the
    ladder is that somebody looks at the metrics between rungs.

    Guardrails are checked when metrics are supplied, and a breach rolls back
    rather than pausing: the recorded threshold already *is* the level at which
    the release is unacceptable.
    """
    from core.db.models.evolution import EvolutionCandidate, EvolutionRelease
    from core.evolution.release import (
        ACTIVE,
        CANARY,
        CANARY_STEPS,
        SHADOW,
        can_promote,
        evaluate_guardrails,
        next_traffic,
        should_auto_rollback,
    )

    if not actor:
        raise ActivationError("actor_required", "推进发布必须署名")

    with SessionLocal() as db:
        release = db.get(EvolutionRelease, release_id)
        if release is None:
            raise ActivationError("release_not_found", f"发布记录不存在: {release_id}")
        if _is_frozen(db):
            raise ActivationError("globally_frozen", "全局冻结开关已打开，禁止推进发布")

        candidate = db.get(EvolutionCandidate, release.candidate_id)
        risk = str(getattr(candidate, "risk_tier", "") or "low")
        guardrails = dict(release.guardrails or {})
        completed: List[str] = list(guardrails.get("completed_verification") or [])
        proposer = str(guardrails.get("proposer") or getattr(candidate, "proposer", "") or "")

        if observed_metrics:
            thresholds = guardrail_thresholds or guardrails.get("thresholds") or {}
            breaches = evaluate_guardrails(observed_metrics, thresholds, window="canary")
            if should_auto_rollback(breaches):
                detail = ", ".join(
                    f"{b.metric}={b.observed}>{b.threshold}" for b in breaches
                )
                db.commit()
                rollback_skill_activation(
                    release_id,
                    actor=actor,
                    reason=f"guardrail breach: {detail}",
                    kind="guardrail",
                )
                return {
                    "release_id": release_id,
                    "stage": "rolled_back",
                    "guardrail_breaches": [b.to_dict() for b in breaches],
                }

        stage = str(release.stage or "")
        traffic = int(release.traffic_percent or 0)

        if stage == SHADOW:
            target_stage, target_traffic = CANARY, CANARY_STEPS[0]
            completed = sorted(set(completed) | {"shadow"})
        elif stage == CANARY and traffic < CANARY_STEPS[-1]:
            target_stage, target_traffic = CANARY, next_traffic(traffic)
        elif stage == CANARY:
            target_stage, target_traffic = ACTIVE, 100
            completed = sorted(set(completed) | {"canary"})
        else:
            raise ActivationError(
                "not_rampable", f"当前阶段 {stage} 无法继续放量"
            )

        allowed, reason = can_promote(
            current_stage=stage,
            target_stage=target_stage,
            risk_tier=risk,
            # ``human_approval`` is this call: a named actor who is not the
            # proposer. Recording it before the check keeps the requirement
            # satisfied by the act itself rather than by a stored flag.
            completed_verification=sorted(set(completed) | {"human_approval"})
            if target_stage == ACTIVE
            else completed,
            proposer=proposer,
            approver=actor,
        )
        # A same-stage traffic bump is not a transition; can_promote would reject
        # canary→canary, which is correct for stages and wrong for ramp steps.
        if not allowed and not (stage == target_stage == CANARY):
            raise ActivationError("promotion_refused", reason)

        if target_stage == ACTIVE:
            _check_pace(db, tenant_id=str((release.scope_filter or {}).get("tenant_id") or "default"))

        release.stage = target_stage
        release.traffic_percent = target_traffic
        guardrails["completed_verification"] = completed
        if target_stage == ACTIVE:
            guardrails["approved_by"] = actor
        release.guardrails = guardrails
        release.approved_by = actor
        if candidate is not None and target_stage == ACTIVE:
            candidate.status = ACTIVE
        db.commit()

        logger.info(
            "[activation] release %s → %s/%d%% by %s",
            release_id,
            target_stage,
            target_traffic,
            actor,
        )
        return {
            "release_id": release_id,
            "stage": target_stage,
            "traffic_percent": target_traffic,
            "completed_verification": completed,
            "exposed_to": (
                "all users in scope"
                if target_stage == ACTIVE
                else f"{target_traffic}% of users in scope"
            ),
        }


def rollback_skill_activation(
    release_id: str, *, actor: str, reason: str, kind: str = "manual"
) -> Dict[str, Any]:
    """Undo a materialisation, restoring the snapshot taken at activation time.

    With no prior version the skill is disabled rather than deleted: history
    referencing it must stay resolvable, and a disabled row is both reversible
    and visible in a way a missing row is not.

    ``kind`` separates "a guardrail caught it" from "a person caught it".
    Collapsing the two would hide how well the evaluation stage is working,
    which is the number that tells you whether the ramp is worth its cost.
    """
    from core.db.models import AdminSkill
    from core.db.models.evolution import EvolutionCandidate, EvolutionRelease
    from core.evolution.release import ROLLBACK_GUARDRAIL, ROLLBACK_MANUAL, ROLLED_BACK

    if not actor or not reason:
        raise ActivationError("audit_required", "回滚必须署名并填写理由")

    with SessionLocal() as db:
        release = db.get(EvolutionRelease, release_id)
        if release is None:
            raise ActivationError("release_not_found", f"发布记录不存在: {release_id}")

        previous = (release.guardrails or {}).get("previous")
        row = (
            db.query(AdminSkill)
            .filter(AdminSkill.skill_id == release.target_asset_id)
            .first()
        )
        if row is not None:
            if previous:
                row.skill_content = previous["skill_content"]
                row.display_name = previous["display_name"]
                row.description = previous["description"]
                row.version = previous["version"]
                row.tags = previous["tags"]
                row.allowed_tools = previous["allowed_tools"]
                row.extra_files = previous["extra_files"]
                row.is_enabled = previous["is_enabled"]
            else:
                row.is_enabled = False

        release.stage = ROLLED_BACK
        release.rolled_back_at = datetime.now(timezone.utc)
        release.rollback_reason = reason
        release.rollback_kind = (
            ROLLBACK_GUARDRAIL if kind == "guardrail" else ROLLBACK_MANUAL
        )

        candidate = db.get(EvolutionCandidate, release.candidate_id)
        if candidate is not None:
            candidate.status = ROLLED_BACK
        db.commit()

    return {
        "release_id": release_id,
        "restored_previous": bool(previous),
        "reason": reason,
    }


def _tools_from_constraints(constraints: Sequence[Dict[str, Any]]) -> List[str]:
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
    from core.evolution.promotion import OrderingConstraint, apply_ordering_constraints

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


def _next_version(previous: Optional[Dict[str, Any]]) -> str:
    if not previous:
        return "1.0.0"
    parts = str(previous.get("version") or "1.0.0").split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except Exception:
        return "1.0.1"


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


# ── Orchestration assets ─────────────────────────────────────────────────────


def materialise_profile_candidate(
    candidate_id: str, *, approver: str, tenant_id: str = "default"
) -> Dict[str, Any]:
    """Publish an approved agent profile.

    Profiles do not ride the shadow → canary ladder that skills do, and the
    reason is not leniency. A profile *is* the assembly, so a canary would mean
    two different assemblies running concurrently under one profile id, with
    every downstream measurement of either one confounded by the other. Instead
    a profile is scoped: it governs one task type, and its blast radius is that
    task type's traffic, recorded on the release row like any other change.

    Rolling back is switching ``is_active`` off, which restores the built-in
    assembly on the next run — no restart, because profiles are read per run.
    """
    from core.db.models.evolution import EvolutionAgentProfile
    from core.evolution.agent_profile import AgentProfile, TARGET_KIND, validate_profile
    from core.evolution.release import ACTIVE as _ACTIVE, new_release_id

    if not approver:
        raise ActivationError("approver_required", "激活必须署名")

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if candidate.target_kind != TARGET_KIND:
            raise ActivationError(
                "unsupported_kind", f"该入口只处理编排候选，收到 {candidate.target_kind}"
            )
        if approver == (candidate.proposer or ""):
            raise ActivationError("self_approval_forbidden", "生成者不能激活自己提出的候选")
        if _is_frozen(db):
            raise ActivationError("layer_frozen", "进化已被全局冻结")

        payload: Dict[str, Any] = {}
        for change in candidate.ir.get("changes") or []:
            if isinstance(change.get("profile"), dict):
                payload = change["profile"]
                break
        if not payload:
            raise ActivationError("no_profile_payload", "候选未携带 profile 定义")

        profile = AgentProfile.from_dict(payload)
        profile.scope = {**(profile.scope or {}), "tenant_id": tenant_id or "default"}

        # Re-validated at activation against the *current* tool grant, not the
        # one that existed when the candidate was written. A grant narrowed in
        # the meantime must not be widened again by approving an older proposal.
        from core.config.catalog import get_enabled_ids

        base_tools = [str(t) for t in get_enabled_ids("tools")] + [
            str(t) for t in get_enabled_ids("mcp")
        ]
        ok, problems = validate_profile(profile, base_tools=base_tools)
        if not ok:
            raise ActivationError("profile_invalid", f"编排定义未通过校验: {problems}")

        previous = db.get(EvolutionAgentProfile, profile.profile_id)
        previous_payload = dict(previous.payload) if previous is not None else None

        # One active profile per (task type, scope). Two would make the
        # resolution order the thing that decides behaviour, which is not a
        # decision anyone reviewed.
        for row in (
            db.query(EvolutionAgentProfile)
            .filter(EvolutionAgentProfile.is_active.is_(True))
            .all()
        ):
            if row.profile_id == profile.profile_id:
                continue
            same_types = set(row.task_types or []) == set(profile.task_types)
            same_scope = dict(row.scope or {}) == dict(profile.scope)
            if same_types and same_scope:
                row.is_active = False

        row = previous or EvolutionAgentProfile(profile_id=profile.profile_id)
        row.version = profile.version
        row.task_types = list(profile.task_types)
        row.scope = dict(profile.scope)
        row.payload = profile.to_dict()
        row.candidate_id = candidate_id
        row.is_active = True
        row.created_by = f"evolution:{approver}"
        if previous is None:
            db.add(row)

        release = EvolutionRelease(
            release_id=new_release_id(),
            candidate_id=candidate_id,
            target_kind=TARGET_KIND,
            target_asset_id=profile.profile_id,
            stage=_ACTIVE,
            traffic_percent=100,
            scope_filter=dict(profile.scope),
            guardrails={"previous": previous_payload, "proposer": candidate.proposer or ""},
            rollback_version_id=(previous_payload or {}).get("version") or "builtin",
            approved_by=approver,
        )
        db.add(release)

        candidate.status = _ACTIVE
        candidate.approved_by = approver
        candidate.approved_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "[activation] profile %s active for %s (tools=%s skills=%s)",
            profile.profile_id,
            profile.task_types or ["*"],
            "narrowed" if profile.tool_allowlist is not None else "unchanged",
            len(profile.skill_ids),
        )
        return {
            "profile_id": profile.profile_id,
            "release_id": release.release_id,
            "task_types": list(profile.task_types),
            "scope": dict(profile.scope),
            "tool_allowlist": profile.tool_allowlist,
            "skill_ids": list(profile.skill_ids),
            "replaced_previous": previous_payload is not None,
        }


def materialise_subagent_candidate(
    candidate_id: str, *, approver: str, owner_user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Create an approved sub-agent, and nothing else.

    Deliberately does **not** install the route that sends work to it. The route
    is a sentence in the parent's prompt, and a sentence in the parent's prompt
    changes every request the parent handles — so it goes through prompt review
    as its own candidate. Creating the agent here and routing to it there means
    approving the agent cannot silently also approve a behaviour change.

    The agent is created disabled for the same reason: an agent nobody routes to
    does nothing, and one that is enabled before its route is reviewed is a
    capability sitting in the product that no reviewer has seen used.
    """
    from core.db.models import UserAgent
    from core.evolution.release import ACTIVE as _ACTIVE, SHADOW, new_release_id

    if not approver:
        raise ActivationError("approver_required", "激活必须署名")

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if candidate.target_kind != "subagent":
            raise ActivationError(
                "unsupported_kind", f"该入口只处理子智能体候选，收到 {candidate.target_kind}"
            )
        if approver == (candidate.proposer or ""):
            raise ActivationError("self_approval_forbidden", "生成者不能激活自己提出的候选")
        if _is_frozen(db):
            raise ActivationError("layer_frozen", "进化已被全局冻结")

        spec: Dict[str, Any] = {}
        for change in candidate.ir.get("changes") or []:
            if change.get("agent_id"):
                spec = change
                break
        if not spec:
            raise ActivationError("no_subagent_payload", "候选未携带子智能体定义")

        # The subset check runs again at activation against the current grant.
        from core.config.catalog import get_enabled_ids

        base_tools = {str(t) for t in get_enabled_ids("tools")} | {
            str(t) for t in get_enabled_ids("mcp")
        }
        requested = {str(t) for t in (spec.get("tools_allowlist") or [])}
        widened = sorted(requested - base_tools)
        if widened:
            raise ActivationError(
                "tool_escalation", f"子智能体工具集超出主体授权: {widened}"
            )
        if spec.get("may_delegate"):
            raise ActivationError("subagent_nesting", "子智能体不得再派生子智能体")

        agent_id = str(spec.get("agent_id"))
        existing = db.get(UserAgent, agent_id)
        if existing is not None:
            raise ActivationError("agent_exists", f"子智能体已存在: {agent_id}")

        agent = UserAgent(
            agent_id=agent_id,
            owner_type="user" if owner_user_id else "admin",
            user_id=owner_user_id,
            name=str(spec.get("name") or agent_id)[:255],
            description=str(spec.get("responsibility") or "")[:2000],
            system_prompt=str(spec.get("responsibility") or ""),
            skill_ids=[str(s) for s in (spec.get("required_skills") or [])],
            mcp_server_ids=sorted(requested),
            kb_ids=[],
            plugin_ids=[],
            max_iters=12,
            timeout=int(spec.get("timeout") or 180),
            # Disabled until its route is approved — see the docstring.
            is_enabled=False,
            extra_config={
                "origin": "evolution",
                "candidate_id": candidate_id,
                "route_hint": str(spec.get("route_hint") or ""),
                "task_types": [str(t) for t in (spec.get("task_types") or [])],
                "tools_allowlist": sorted(requested),
                "may_delegate": False,
            },
        )
        db.add(agent)

        release = EvolutionRelease(
            release_id=new_release_id(),
            candidate_id=candidate_id,
            target_kind="subagent",
            target_asset_id=agent_id,
            # Reaches nobody until the route exists, which is what shadow means.
            stage=SHADOW,
            traffic_percent=0,
            scope_filter={"owner_user_id": owner_user_id} if owner_user_id else {},
            guardrails={"proposer": candidate.proposer or "", "route_pending": True},
            rollback_version_id="v0",
            approved_by=approver,
        )
        db.add(release)

        candidate.approved_by = approver
        candidate.approved_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "[activation] subagent %s created (disabled, route pending) tools=%s",
            agent_id,
            sorted(requested),
        )
        return {
            "agent_id": agent_id,
            "release_id": release.release_id,
            "stage": SHADOW,
            "is_enabled": False,
            "tools_allowlist": sorted(requested),
            "route_hint": str(spec.get("route_hint") or ""),
            "next_step": "路由句需作为提示词候选单独审批后，子智能体才会被启用",
        }


def retire_skill_candidate(candidate_id: str, *, approver: str) -> Dict[str, Any]:
    """Take an approved retirement into effect.

    Retirement is disabling plus a release row, never a delete. Three reasons it
    is done this way: the evidence behind the decision stays inspectable, the
    skill can be brought back in one operation if the decision was wrong, and
    anything built on top of it — a profile naming it, a sub-agent requiring it
    — can be found and invalidated rather than silently pointing at nothing.
    """
    from core.db.models import AdminSkill
    from core.db.models.evolution import EvolutionAgentProfile
    from core.evolution.release import RETIRED, new_release_id

    if not approver:
        raise ActivationError("approver_required", "退役必须署名")

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if candidate.operation != "deprecate":
            raise ActivationError("not_a_retirement", "该候选不是退役候选")
        if approver == (candidate.proposer or ""):
            raise ActivationError("self_approval_forbidden", "生成者不能批准自己提出的退役")

        skill_id = candidate.target_asset_id
        row = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
        if row is None:
            raise ActivationError("skill_not_found", f"技能不存在: {skill_id}")

        previous_enabled = bool(row.is_enabled)
        row.is_enabled = False

        # Cross-layer: anything assembled on top of this skill stops being
        # applied. Leaving them active would leave a profile promising a
        # capability that is no longer loadable, which fails silently.
        invalidated: List[str] = []
        for profile_row in (
            db.query(EvolutionAgentProfile)
            .filter(EvolutionAgentProfile.is_active.is_(True))
            .all()
        ):
            payload = profile_row.payload or {}
            if skill_id in (payload.get("skill_ids") or []):
                profile_row.is_active = False
                invalidated.append(profile_row.profile_id)

        release = EvolutionRelease(
            release_id=new_release_id(),
            candidate_id=candidate_id,
            target_kind="skill",
            target_asset_id=skill_id,
            stage=RETIRED,
            traffic_percent=0,
            scope_filter={},
            guardrails={
                "previous_enabled": previous_enabled,
                "invalidated_profiles": invalidated,
                "proposer": candidate.proposer or "",
            },
            rollback_version_id=row.version or "",
            approved_by=approver,
        )
        db.add(release)

        candidate.status = "active"
        candidate.approved_by = approver
        candidate.approved_at = datetime.now(timezone.utc)
        db.commit()

        # Restore the memories this skill was compiled from: the knowledge lost
        # its cheaper carrier, so it goes back to being paid for by retrieval.
        from core.evolution.memory_apply import revert_memory_ops

        source_candidate = _source_candidate_for(db, skill_id)
        restored = revert_memory_ops(candidate_id=source_candidate) if source_candidate else {}

        logger.info(
            "[activation] retired %s (profiles invalidated=%s memories restored=%d)",
            skill_id,
            invalidated,
            len(restored.get("reverted", [])),
        )
        return {
            "skill_id": skill_id,
            "release_id": release.release_id,
            "invalidated_profiles": invalidated,
            "restored_memory_ops": restored.get("reverted", []),
        }


def _source_candidate_for(db, skill_id: str) -> str:
    """The candidate that first materialised this skill.

    Memory demotions are recorded against that candidate, so it is the key that
    finds them again when the skill is retired.
    """
    row = (
        db.query(EvolutionRelease)
        .filter(
            EvolutionRelease.target_kind == "skill",
            EvolutionRelease.target_asset_id == skill_id,
        )
        .order_by(EvolutionRelease.created_at.asc())
        .first()
    )
    return str(row.candidate_id) if row is not None else ""
