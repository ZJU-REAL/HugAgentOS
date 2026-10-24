"""Review an answer against the ontology, repair it, and audit the outcome."""
from __future__ import annotations
import asyncio
import time
from collections import Counter
from typing import Any
from core.ontology.revision import is_substantive_revision
from core.ontology.runtime_gates import evaluate_output
from core.infra.logging import get_logger
from core.ontology import manual_review
from core.ontology import review_operations
from core.ontology import reviewer_state

logger = get_logger(__name__)

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
    remediate: reviewer_state.RemediationCallback | None = None,
    max_repair_attempts: int = reviewer_state.DEFAULT_MAX_REPAIR_ATTEMPTS,
    capability_scope: str = "",
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
            "manual_review": manual_review.build_manual_review(
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
        "capability_scope": capability_scope,
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
            reviewer_state.PERSPECTIVES[index % len(reviewer_state.PERSPECTIVES)] + f"（席位 {index + 1}）"
            for index in range(committee_size)
        ]
        reviewers = await asyncio.gather(
            *(review_operations.review_once(perspective, **common) for perspective in perspectives)
        )
        counts = Counter(item["verdict"] for item in reviewers)
        verdict, votes = counts.most_common(1)[0]
        if votes <= committee_size // 2:
            verdict = "escalate"
    else:
        reviewers = [await review_operations.review_once(reviewer_state.PERSPECTIVES[0], **common)]
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
                        "feedback": manual_review.dedupe_strings(round_feedback),
                        "affected_claims": round_claims,
                        "citations": list(citations),
                    }
                )
            elif deterministic.allowed:
                revised = await review_operations.revise_answer(
                    task=task,
                    answer=current_answer,
                    feedback=manual_review.dedupe_strings(round_feedback),
                    user_id=user_id,
                    model_name=model_name,
                    model_provider_id=model_provider_id,
                    runtime=runtime,
                    capability_scope=capability_scope,
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
    review_card = manual_review.build_manual_review(
        required=annotated,
        violations=final_violations,
        affected_claims=final_affected_claims,
        suggestions=final_suggestions or all_feedback,
    )
    new_tools = manual_review.dedupe_strings(
        [
            str(item.get("tool_name") or "")
            for item in trace[initial_trace_len:]
            if item.get("type") == "tool_result"
        ]
    )
    evidence = manual_review.dedupe_strings(all_evidence)
    feedback = manual_review.dedupe_strings(all_feedback + final_suggestions)
    latency_ms = round((time.monotonic() - started) * 1000)
    try:
        from core.ontology.audit_events import record_enforcement_event, record_review_run

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
            from core.ontology.evolution import schedule_ontology_evolution

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
        "manual_review": review_card,
        "latency_ms": latency_ms,
        "revised": current_answer != original_answer,
        "annotated": annotated,
        "repair_attempts": repair_attempts,
        "attempts": max(1, len(review_rounds)),
        "review_rounds": review_rounds,
        "new_tools": new_tools,
        "new_citation_count": max(0, len(citations) - initial_citation_len),
    }
