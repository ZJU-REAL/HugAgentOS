"""Claim and complete the single final-answer review of a governance run."""
from __future__ import annotations
from typing import Any

def requires_output_review(runtime: dict[str, Any]) -> bool:
    """Whether an L-b/L-c review must run after the streamed draft."""
    return bool(
        runtime.get("enabled")
        and (
            runtime.get("review_level") != "none"
            or any(
                (rule.get("target") or {}).get("kind") == "output"
                for pack in runtime.get("packs", [])
                for rule in pack.get("constraints", [])
            )
        )
    )


def claim_output_review(runtime: dict[str, Any], *, owner: str) -> bool:
    """Atomically claim the single final-answer review for one governance run."""
    if not requires_output_review(runtime):
        return False
    state = runtime.setdefault(
        "output_review",
        {"status": "pending", "owner": None, "count": 0},
    )
    if state.get("status") in {"running", "completed"}:
        return False
    state.update({"status": "running", "owner": owner})
    return True


def complete_output_review(
    runtime: dict[str, Any],
    *,
    owner: str,
    verdict: str,
    attempts: int = 1,
) -> None:
    """Mark the claimed review complete and retain its single-run evidence."""
    state = runtime.setdefault("output_review", {})
    if state.get("status") != "running" or state.get("owner") != owner:
        return
    count = int(state.get("count") or 0) + max(1, int(attempts or 1))
    state.update(
        {
            "status": "completed",
            "owner": owner,
            "count": count,
            "verdict": verdict,
        }
    )


def release_output_review(runtime: dict[str, Any], *, owner: str) -> None:
    """Release a failed claim so a safe retry can review the final answer."""
    state = runtime.setdefault("output_review", {})
    if state.get("status") == "running" and state.get("owner") == owner:
        state.update({"status": "pending", "owner": None})
