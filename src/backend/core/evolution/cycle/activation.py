"""Materialise an approved skill candidate at the release stage it has earned."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from core.db.engine import SessionLocal
import logging
from core.evolution.cycle import activation_policy
from core.evolution.cycle import activation_constants
from core.evolution.cycle import activation_types
from core.evolution.cycle import skill_document

logger = logging.getLogger(__name__)

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
    from core.evolution.cycle.release import ACTIVE, IN_FLIGHT_STAGES, REPLAY_PASSED, new_release_id

    with SessionLocal() as db:
        candidate = db.get(EvolutionCandidate, candidate_id)
        if candidate is None:
            raise activation_types.ActivationError("candidate_not_found", f"候选不存在: {candidate_id}")
        if candidate.target_kind != "skill":
            raise activation_types.ActivationError(
                "unsupported_kind", f"暂不支持物化 {candidate.target_kind} 候选"
            )

        # Separation of duties. No force path — see the docstring.
        if approver and approver == (candidate.proposer or ""):
            raise activation_types.ActivationError(
                "self_approval_forbidden", "生成者不能激活自己提出的候选"
            )
        if not approver:
            raise activation_types.ActivationError("approver_required", "激活必须署名")

        stage, traffic = activation_policy.entry_stage(
            candidate, approver=approver, force=force, owner_user_id=owner_user_id
        )
        # Pace and freeze apply to anything that becomes live immediately. A
        # shadow entry changes nothing users can see, so holding it back would
        # only delay measurement.
        if stage == ACTIVE and not owner_user_id:
            activation_policy.check_pace(db, tenant_id=tenant_id)

        ir = candidate.ir or {}
        changes = ir.get("changes") or []
        tools: List[str] = []
        constraints: List[Dict[str, Any]] = []
        document: Optional[Dict[str, Any]] = None
        manifest: Optional[Dict[str, Any]] = None
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
            declared = change.get("manifest")
            if isinstance(declared, dict) and declared:
                manifest = declared
            for item in change.get("source_memories") or []:
                if isinstance(item, dict) and item.get("ref"):
                    source_memories[str(item["ref"])] = str(item.get("user_id") or "")

        # 图谱证据自相矛盾的候选可以存在、可以进 shadow 观测，但不允许自动
        # 走到 ACTIVE：冲突意味着「适用条件」本身不可信，必须先由人裁决。
        if manifest and manifest.get("graph_conflict") and stage == ACTIVE:
            raise activation_types.ActivationError(
                "graph_conflict_requires_review",
                f"L3 图谱证据存在冲突，禁止直接激活：{manifest.get('graph_conflicts')}",
            )

        skill_id = candidate.target_asset_id
        if not skill_id.startswith(activation_constants.EVOLUTION_PREFIX):
            skill_id = activation_constants.EVOLUTION_PREFIX + skill_id.replace("auto-", "")
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
            from core.evolution.generators.skill_drafting import render_skill_markdown, validate_draft

            tools = [str(t) for t in (document.get("allowed_tools") or [])]
            display_name = str(document.get("display_name") or "")
            description = str(document.get("description") or "")
            body = skill_document.body_of(str(document.get("content") or ""))
            violations = validate_draft(
                skill_id=skill_id,
                display_name=display_name,
                description=description,
                body=body,
                allowed_tools=tools,
            )
            if violations:
                raise activation_types.ActivationError(
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
                tools = skill_document.tools_from_constraints(constraints)
            if not tools:
                raise activation_types.ActivationError(
                    "no_tool_sequence", "候选既未声明工具顺序、也未携带顺序约束，无法物化"
                )
            display_name = skill_document.skill_label(tools, rules_only=rules_only)
            description = skill_document.skill_description(tools, constraints, rules_only=rules_only)
            content = skill_document.skill_markdown(
                skill_id=skill_id,
                label=display_name,
                description=description,
                tools=tools,
                evidence=len(candidate.evidence_refs or []),
                constraints=constraints,
                rationale=candidate.hypothesis or "",
                rules_only=rules_only,
            )

        previous = activation_policy.capture_previous(db, skill_id)

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
            raise activation_types.ActivationError(
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
        row.version = activation_policy.next_version(previous)
        row.tags = sorted(set((previous or {}).get("tags", [])) | {activation_constants.EVOLUTION_TAG})
        # The distilled ordering, in the shape the runtime reads.
        extra_files = {
            **(previous or {}).get("extra_files", {}),
            "tool_sequence.json": activation_policy.json(tools),
            # The transferable part. A consumer that only understands sequences
            # still works; one that understands rules gets the wider benefit.
            "ordering_constraints.json": activation_policy.json(constraints),
        }
        if manifest:
            # 机器读 manifest（边界/依赖/证据引用），Agent 读 SKILL.md —— 两者
            # 职责分离。作用域以本次激活实际发生的为准，而不是候选申报的。
            extra_files["evolution_manifest.json"] = activation_policy.json(
                {
                    **manifest,
                    "scope": "personal" if owner_user_id else str(
                        manifest.get("scope") or "workspace"
                    ),
                }
            )
        row.extra_files = extra_files
        row.allowed_tools = tools
        row.is_enabled = True

        # The scope the IR asked for, now actually enforced at load time by
        # core.evolution.evidence.exposure. Recording it here rather than deriving it
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
        from core.evolution.generators.memory_apply import demote_promoted_memories

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

    # 血缘落账：这一版技能由哪些 L2 记忆编译（compiled_from）、被哪些 L3 关系
    # 约束（constrained_by）、由哪些 Episode 验证（validated_by）。尽力而为——
    # 账本失败绝不回滚一次已经成立的激活。
    lineage_written = 0
    try:
        from core.evolution.evidence import lineage

        with SessionLocal() as db:
            refreshed = db.get(EvolutionCandidate, candidate_id)
            pack_id = str(getattr(refreshed, "evidence_pack_id", "") or "")
        pack = lineage.load_evidence_pack(pack_id) if pack_id else None
        if pack is not None:
            lineage_written = len(
                lineage.record_links(
                    lineage.links_for_skill(
                        pack, skill_id=skill_id, skill_version=new_version
                    ),
                    candidate_id=candidate_id,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[activation] lineage recording skipped: %s", exc)

    return {
        "skill_id": skill_id,
        "release_id": release_id,
        "stage": stage,
        "traffic_percent": traffic,
        "scope": scope_filter,
        "demoted_memories": demoted,
        "lineage_links": lineage_written,
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
