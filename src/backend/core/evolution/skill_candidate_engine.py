"""统一技能候选引擎 — 所有技能候选的唯一生产入口（P2/P3）.

在此之前，技能候选有两条平行的构造路径：意图聚类（loop 的 intent 路径，LLM
写文档）和重复工具序列（loop 的 promotion 路径，只存序列）。同类证据从两条
路径进来会产出结构不同的技能——一条有 manifest 一条没有、校验一条严一条松。

这里把两条路径收敛到同一条编译链：

    证据 views → Evidence Resolver（不可变 pack）
              → build_manifest（代码生成确定性结构：工具/边界/依赖/风险）
              → （可选）LLM 撰写 SKILL.md（skill_gen 三段式）
              → validate_candidate_structure（分层边界强制校验）
              → EvolutionIR → validate_ir（安全不变量）
              → 持久化（candidate + evidence_pack + credit decision）

LLM 只出现在其中一步，且只写正文；工具授权、作用域、证据来源、发布风险全部
由代码决定——这是「两段式生成」的边界所在。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.evolution.evidence_contract import (
    CapabilityEvidencePackV2,
    build_manifest,
    validate_candidate_structure,
)
from core.evolution.evidence_resolver import resolve_capability_pack
from core.evolution.ir import (
    RISK_HIGH,
    EvolutionIR,
    InvariantViolation,
    Rollback,
    new_candidate_id,
    validate_ir,
)

logger = logging.getLogger(__name__)

STATUS_CREATED = "created"
STATUS_DRY_RUN = "dry_run"
STATUS_NOT_WRITTEN = "not_written"
STATUS_REJECTED = "rejected"


@dataclass
class SkillCandidateSpec:
    """One skill candidate, described by its evidence rather than its shape."""

    asset_id: str
    proposer: str
    hypothesis: str
    views: List[Dict[str, Any]] = field(default_factory=list)
    intent_cluster: Dict[str, Any] = field(default_factory=dict)
    procedures: List[Dict[str, Any]] = field(default_factory=list)
    tool_sequence: List[str] = field(default_factory=list)
    ordering_constraints: List[Dict[str, Any]] = field(default_factory=list)
    operation: str = "new"
    base_version: str = "v0"
    risk_tier: str = RISK_HIGH
    scope: Dict[str, str] = field(default_factory=lambda: {"level": "workspace"})
    tenant_id: str = "default"
    # 意图路径写文档；序列/规则路径不写（它们的价值在结构，不在正文）。
    write_document: bool = True
    rollback_triggers: List[str] = field(
        default_factory=lambda: ["success -3pp", "risk_denial_rate +2pp"]
    )


def create_skill_candidate(
    spec: SkillCandidateSpec,
    *,
    credit: Dict[str, Any],
    dry_run: bool = False,
    run_async: Optional[Callable[[Any], Any]] = None,
) -> Dict[str, Any]:
    """Compile one skill candidate through the unified chain.

    Returns a result dict with ``status`` ∈ {created, dry_run, not_written,
    rejected} — callers format their own report entries from it. Never raises:
    this runs inside a background cycle where one bad candidate must not cost
    the rest of the batch.
    """
    pack = resolve_capability_pack(
        views=spec.views,
        intent_cluster=spec.intent_cluster,
        procedures=spec.procedures,
        ordering_constraints=spec.ordering_constraints,
        scope=spec.scope,
        tenant_id=spec.tenant_id,
    )
    # 序列路径的工具来自模式本身而非成功 Episode 的并集时，以模式为准，
    # 但仍必须是 Episode 证据的子集——这在结构校验里强制。
    if spec.tool_sequence and not pack.required_tools:
        pack.required_tools = [str(t) for t in spec.tool_sequence]

    scope_level = str(spec.scope.get("level") or "workspace")
    manifest = build_manifest(pack, scope_level=scope_level, risk_tier=spec.risk_tier)

    document: Optional[Dict[str, Any]] = None
    draft_info: Dict[str, Any] = {}
    if spec.write_document:
        if run_async is None:
            return {"status": STATUS_REJECTED, "code": "no_async_runner"}
        import core.evolution.skill_gen as skill_gen

        document, draft = run_async(
            skill_gen.generate_skill_document(
                skill_id=spec.asset_id,
                episodes=spec.views,
                ordering_constraints=spec.ordering_constraints,
                procedures=[
                    {
                        "rule": p.get("rule", ""),
                        "why": p.get("why", ""),
                        "applies_to": p.get("applies_to", ""),
                    }
                    for p in spec.procedures
                ],
                rationale=spec.hypothesis,
            )
        )
        draft_info = draft.to_dict()
        if document is None:
            return {
                "status": STATUS_NOT_WRITTEN,
                "why_not": draft_info,
                "pack_id": pack.pack_id,
            }

    document_tools = (
        [str(t) for t in (document.get("allowed_tools") or [])]
        if document
        else [str(t) for t in spec.tool_sequence]
    )
    body = str((document or {}).get("content") or "")
    violations = validate_candidate_structure(
        pack=pack,
        manifest=manifest,
        document_tools=document_tools,
        body=body,
        scope_level=scope_level,
    )
    if violations:
        logger.info(
            "[skill-engine] candidate %s rejected by structure validation: %s",
            spec.asset_id,
            violations,
        )
        return {
            "status": STATUS_REJECTED,
            "code": "structure_violation",
            "violations": violations,
            "pack_id": pack.pack_id,
        }

    change: Dict[str, Any] = {
        "tool_sequence": [] if document else [str(t) for t in spec.tool_sequence],
        "ordering_constraints": [dict(r) for r in spec.ordering_constraints],
        "manifest": manifest,
    }
    if document is not None:
        change["document"] = document
        change["source_memories"] = [
            {
                "ref": str((p.get("ref") or {}).get("external_id") or ""),
                "user_id": str(p.get("user_id") or ""),
            }
            for p in pack.procedures
            if (p.get("ref") or {}).get("external_id")
        ]

    episode_ids = [str(e.get("episode_id") or "") for e in pack.episodes if e.get("episode_id")]
    ir = EvolutionIR(
        target_kind="skill",
        target_asset_id=spec.asset_id,
        base_version=spec.base_version,
        operation=spec.operation,
        hypothesis=spec.hypothesis,
        changes=[change],
        evidence_refs=episode_ids[:20],
        requested_tools=document_tools,
        risk_tier=spec.risk_tier,
        scope=dict(spec.scope),
        rollback=Rollback(version_id=spec.base_version, triggers=list(spec.rollback_triggers)),
        evaluation_plan={
            "replay_set": "auto_holdout",
            "primary_metric": "task_success",
            # Replay 覆盖要求由 pack 派生，评估器据此拒绝覆盖不足的回放集。
            "coverage": {
                "needs_failure_case": bool(pack.failure_recoveries),
                "needs_negative_example": bool(pack.negative_examples),
                "needs_graph_dependency_case": bool(manifest.get("dependencies")),
            },
        },
    )
    try:
        validate_ir(
            ir,
            credit_decision=credit,
            base_tools=document_tools if document else None,
        )
    except InvariantViolation as violation:
        return {
            "status": STATUS_REJECTED,
            "code": violation.code,
            "pack_id": pack.pack_id,
        }

    if dry_run:
        return {"status": STATUS_DRY_RUN, "pack_id": pack.pack_id, "draft": draft_info}

    candidate_id = _persist(ir, pack=pack, credit=credit, proposer=spec.proposer)
    if not candidate_id:
        return {"status": STATUS_REJECTED, "code": "persist_failed", "pack_id": pack.pack_id}
    return {
        "status": STATUS_CREATED,
        "candidate_id": candidate_id,
        "pack_id": pack.pack_id,
        "pack_hash": pack.content_hash(),
        "attempts": int(draft_info.get("attempts") or 0),
        "manifest": manifest,
    }


def _persist(
    ir: EvolutionIR,
    *,
    pack: CapabilityEvidencePackV2,
    credit: Dict[str, Any],
    proposer: str,
) -> Optional[str]:
    """Candidate + pack + credit decision, in that dependency order.

    The pack and the decision are content-addressed, so a duplicate candidate
    (same checksum) simply reuses the rows the first one wrote.
    """
    from core.db.engine import SessionLocal
    from core.db.models.evolution import EvolutionCandidate
    from core.evolution import lineage
    from core.evolution.release import DRAFT

    pack_outcome = lineage.persist_evidence_pack(pack)
    decision_id = lineage.record_credit_decision(
        credit, episode_id=(ir.evidence_refs[0] if ir.evidence_refs else "")
    )
    stored_credit = {**credit, "decision_id": decision_id}

    checksum = ir.change_checksum()
    try:
        with SessionLocal() as db:
            existing = (
                db.query(EvolutionCandidate)
                .filter(
                    EvolutionCandidate.target_kind == ir.target_kind,
                    EvolutionCandidate.target_asset_id == ir.target_asset_id,
                    EvolutionCandidate.base_version_id == (ir.base_version or None),
                    EvolutionCandidate.change_checksum == checksum,
                )
                .first()
            )
            if existing is not None:
                # 同一修改再次被提出：一条评审项，不是两条。pack 引用留旧值，
                # 因为旧行的证据仍然可解析。
                return existing.candidate_id

            candidate_id = new_candidate_id()
            db.add(
                EvolutionCandidate(
                    candidate_id=candidate_id,
                    target_kind=ir.target_kind,
                    target_asset_id=ir.target_asset_id,
                    base_version_id=ir.base_version or None,
                    operation=ir.operation,
                    ir=ir.to_dict(),
                    hypothesis=ir.hypothesis,
                    change_checksum=checksum,
                    evidence_refs=list(ir.evidence_refs),
                    evidence_pack_id=pack_outcome.get("pack_id") or None,
                    credit_decision=stored_credit,
                    risk_tier=ir.risk_tier,
                    scope=ir.scope,
                    status=DRAFT,
                    proposer=proposer,
                )
            )
            db.commit()
            return candidate_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("[skill-engine] persisting candidate failed: %s", exc)
        return None
