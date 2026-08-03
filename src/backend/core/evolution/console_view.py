"""Console serialisation for evolution objects (GCE tickets 23–26).

Pure projection: no auth, no FastAPI, no database session. It lives outside the
route module because the route module imports ``api.deps``, and ``api/__init__``
pulls in ``api.app`` — which is what registers the routes in the first place.
Anything importing a serialiser would therefore have to drag the whole
application in behind it, and importing it *before* the app inverts the
registration order.

Keeping the projection here means tests, the worker and the console all share
one definition of what a candidate looks like, and none of them need the web
layer to find out.
"""

from typing import Any, Dict

from core.db.models.evolution import (
    EvolutionCandidate,
    EvolutionEpisode,
    EvolutionRelease,
)


def episode_to_dict(row: EvolutionEpisode) -> Dict[str, Any]:
    return {
        "episode_id": row.episode_id,
        "run_id": row.run_id,
        "chat_id": row.chat_id,
        "task_type": row.task_type,
        "objective_preview": row.objective_preview,
        "asset_bundle_id": row.asset_bundle_id,
        # Surfaced so the console can explain why replay is unavailable rather
        # than showing a button that always fails.
        "bundle_partial": bool(row.bundle_partial),
        "backfilled": bool(row.backfilled),
        "replay_eligible": bool(
            row.asset_bundle_id and not row.bundle_partial and not row.backfilled
        ),
        "quality_score": row.quality_score,
        "cost_usd": row.cost_usd,
        "risk_result": row.risk_result,
        "event_count": row.event_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def candidate_to_dict(row: EvolutionCandidate, *, full: bool = False) -> Dict[str, Any]:
    credit = row.credit_decision or {}
    payload = {
        "candidate_id": row.candidate_id,
        "target_kind": row.target_kind,
        "target_asset_id": row.target_asset_id,
        "operation": row.operation,
        "status": row.status,
        "risk_tier": row.risk_tier,
        # The console leads with *why*, so the reason is a first-class field
        # rather than something buried inside the IR blob.
        #
        # The hypothesis wins over the attribution explanation. Attribution
        # names a *category* ("过程停滞：重复动作、无产出增量") and the same
        # sentence therefore appears on every candidate that shares a target —
        # while the hypothesis says what was actually observed ("chat 类任务的
        # 16 条执行中，8 个工具一次都没被用过"). Showing the category in the one
        # column a reviewer reads first made every orchestration candidate look
        # identical and hid the only line that could inform the decision. The
        # category is still available, in ``attributed_to``.
        "why": row.hypothesis or credit.get("explanation"),
        "attributed_to": credit.get("selected"),
        "confidence": credit.get("confidence"),
        "proposer": row.proposer,
        "approved_by": row.approved_by,
        "evidence_count": len(row.evidence_refs or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if full:
        payload.update(
            {
                "hypothesis": row.hypothesis,
                "credit_decision": credit,
                "evidence_refs": row.evidence_refs or [],
                "scope": row.scope or {},
                "reject_reason": row.reject_reason,
                # Technical detail last: the reviewer should not need to read
                # JSON before they can start judging.
                "ir": row.ir,
            }
        )
    return payload


def release_to_dict(row: EvolutionRelease) -> Dict[str, Any]:
    return {
        "release_id": row.release_id,
        "candidate_id": row.candidate_id,
        "target_kind": row.target_kind,
        "target_asset_id": row.target_asset_id,
        "stage": row.stage,
        "traffic_percent": row.traffic_percent,
        "guardrails": row.guardrails or {},
        # Recorded at promotion time so the board can always show where a
        # rollback would land, before anything goes wrong.
        "rollback_version_id": row.rollback_version_id,
        "rollback_kind": row.rollback_kind,
        "rollback_reason": row.rollback_reason,
        "approved_by": row.approved_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


