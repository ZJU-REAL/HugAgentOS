"""证据与血缘账本 — pack / 归因决策 / 晋升链的持久化（compiler V2, P1）.

三个持久化面共享同一批设计约束：

* **幂等**。所有 id 都由内容派生：同一 pack、同一决策、同一条血缘边重复写入
  落在同一行上，重复运行一个 cycle 不会让账本膨胀。
* **尽力而为**。账本是可解释性基础设施，不是可用性风险——任何一次写入失败都
  只降级为日志，绝不让候选生成或激活失败。
* **不可变**。pack 与决策一经写入不再更新；血缘边只增不改。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.evolution.evidence_contract import CapabilityEvidencePackV2

logger = logging.getLogger(__name__)

# ── Promotion link relations ─────────────────────────────────────────────────

REL_COMPILED_FROM = "compiled_from"      # L2 memory → skill
REL_CONSTRAINED_BY = "constrained_by"    # L3 relation → skill
REL_VALIDATED_BY = "validated_by"        # episode → skill
REL_ASSEMBLED_INTO = "assembled_into"    # skill → agent profile
REL_ROLLED_BACK_TO = "rolled_back_to"    # skill version → prior version
REL_PERSONALIZED_BY = "personalized_by"  # L1 policy → skill (runtime binding)

LINK_RELATIONS = frozenset(
    {
        REL_COMPILED_FROM,
        REL_CONSTRAINED_BY,
        REL_VALIDATED_BY,
        REL_ASSEMBLED_INTO,
        REL_ROLLED_BACK_TO,
        REL_PERSONALIZED_BY,
    }
)


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ── Evidence packs ───────────────────────────────────────────────────────────


def persist_evidence_pack(pack: CapabilityEvidencePackV2) -> Dict[str, Any]:
    """Store an immutable pack; same content → same row, never a duplicate."""
    pack_hash = pack.content_hash()
    pack_id = pack.pack_id
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionEvidencePack

        with SessionLocal() as db:
            existing = db.get(EvolutionEvidencePack, pack_id)
            if existing is None:
                db.add(
                    EvolutionEvidencePack(
                        pack_id=pack_id,
                        schema_version=pack.schema_version,
                        pack_hash=pack_hash,
                        scope=pack.candidate_scope,
                        pack=pack.to_dict(),
                        support=pack.support,
                    )
                )
                db.commit()
                created = True
            else:
                created = False
        return {"pack_id": pack_id, "pack_hash": pack_hash, "created": created}
    except Exception as exc:  # noqa: BLE001 - the ledger must never fail the flow
        logger.debug("[lineage] evidence pack persist skipped: %s", exc)
        return {"pack_id": pack_id, "pack_hash": pack_hash, "created": False}


def load_evidence_pack(pack_id: str) -> Optional[CapabilityEvidencePackV2]:
    if not pack_id:
        return None
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionEvidencePack

        with SessionLocal() as db:
            row = db.get(EvolutionEvidencePack, pack_id)
            if row is None:
                return None
            return CapabilityEvidencePackV2.from_dict(row.pack or {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("[lineage] evidence pack load failed: %s", exc)
        return None


# ── Credit decisions ─────────────────────────────────────────────────────────


def record_credit_decision(credit: Dict[str, Any], *, episode_id: str = "") -> str:
    """Persist one attribution verdict; returns its stable ``decision_id``.

    The id is content-derived: the same verdict recorded twice (a retried
    cycle, a re-run engine) is one row. Candidates reference this id, so the
    stored copy inside the candidate becomes a cache, not the only record.
    """
    canonical = json.dumps(
        {
            "selected": credit.get("selected"),
            "verdict": credit.get("verdict"),
            "scores": credit.get("scores"),
            "features": credit.get("features"),
            "assigner_version": credit.get("assigner_version"),
            "episode_id": episode_id,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    decision_id = "cred_" + _digest(canonical)[:24]
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionCreditDecision

        with SessionLocal() as db:
            if db.get(EvolutionCreditDecision, decision_id) is None:
                db.add(
                    EvolutionCreditDecision(
                        decision_id=decision_id,
                        episode_id=episode_id or None,
                        selected=str(credit.get("selected") or "no_update"),
                        verdict=str(credit.get("verdict") or ""),
                        confidence=float(credit.get("confidence") or 0.0),
                        scores=credit.get("scores") or {},
                        features=credit.get("features") or {},
                        explanation=str(credit.get("explanation") or "")[:2000],
                        assigner_version=str(credit.get("assigner_version") or ""),
                    )
                )
                db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[lineage] credit decision persist skipped: %s", exc)
    return decision_id


# ── Promotion links ──────────────────────────────────────────────────────────


def record_links(
    edges: Sequence[Tuple[str, str, str, str, str]],
    *,
    candidate_id: str = "",
) -> List[str]:
    """Write lineage edges ``(source_kind, source_id, relation, target_kind, target_id)``.

    Idempotent per edge; unknown relations are refused loudly rather than
    stored, because a lineage graph with free-text edge labels answers nothing.
    """
    written: List[str] = []
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionPromotionLink

        with SessionLocal() as db:
            for source_kind, source_id, relation, target_kind, target_id in edges:
                if relation not in LINK_RELATIONS:
                    logger.warning("[lineage] unknown link relation refused: %s", relation)
                    continue
                if not source_id or not target_id:
                    continue
                link_id = "plink_" + _digest(
                    source_kind, source_id, relation, target_kind, target_id
                )[:24]
                if db.get(EvolutionPromotionLink, link_id) is None:
                    db.add(
                        EvolutionPromotionLink(
                            link_id=link_id,
                            source_kind=source_kind,
                            source_id=source_id,
                            relation=relation,
                            target_kind=target_kind,
                            target_id=target_id,
                            candidate_id=candidate_id or None,
                        )
                    )
                written.append(link_id)
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[lineage] link persist skipped: %s", exc)
        return []
    return written


def links_for_skill(
    pack: CapabilityEvidencePackV2, *, skill_id: str, skill_version: str
) -> List[Tuple[str, str, str, str, str]]:
    """The full lineage a materialised skill owes its evidence pack."""
    target = f"{skill_id}@{skill_version}" if skill_version else skill_id
    edges: List[Tuple[str, str, str, str, str]] = []
    for procedure in pack.procedures:
        ref = procedure.get("ref") or {}
        source_id = str(ref.get("external_id") or ref.get("content_hash") or "")
        if source_id:
            edges.append(("memory", source_id, REL_COMPILED_FROM, "skill", target))
    for relation in pack.graph_context:
        relation_id = str(relation.get("relation_id") or "")
        if relation_id:
            edges.append(("graph_relation", relation_id, REL_CONSTRAINED_BY, "skill", target))
    for episode in pack.episodes:
        episode_id = str(episode.get("episode_id") or "")
        if episode_id:
            edges.append(("episode", episode_id, REL_VALIDATED_BY, "skill", target))
    for policy in pack.profile_policy_refs:
        policy_id = str(policy.get("policy") or "")
        if policy_id:
            edges.append(("profile_policy", policy_id, REL_PERSONALIZED_BY, "skill", target))
    return edges


def lineage_of(target_kind: str, target_id: str) -> List[Dict[str, Any]]:
    """Every recorded edge touching one asset, for the console."""
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionPromotionLink
        from sqlalchemy import or_

        with SessionLocal() as db:
            rows = (
                db.query(EvolutionPromotionLink)
                .filter(
                    or_(
                        (EvolutionPromotionLink.target_kind == target_kind)
                        & (EvolutionPromotionLink.target_id.like(f"{target_id}%")),
                        (EvolutionPromotionLink.source_kind == target_kind)
                        & (EvolutionPromotionLink.source_id.like(f"{target_id}%")),
                    )
                )
                .order_by(EvolutionPromotionLink.created_at.asc())
                .all()
            )
            return [
                {
                    "source_kind": row.source_kind,
                    "source_id": row.source_id,
                    "relation": row.relation,
                    "target_kind": row.target_kind,
                    "target_id": row.target_id,
                    "candidate_id": row.candidate_id or "",
                }
                for row in rows
            ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[lineage] lineage query failed: %s", exc)
        return []
