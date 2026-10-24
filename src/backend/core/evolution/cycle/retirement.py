"""Find assets to retire and activations that caused negative transfer."""
from __future__ import annotations
from typing import Any, Dict, List, Sequence
from core.evolution.cycle import promotion_constants
from core.evolution.cycle import promotion_types

def find_retirement_candidates(
    usage: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Assets that are not earning their place, and what to do about each.

    ``usage`` per asset:

    ``offers``
        turns where the skill was selected into the prompt (non-degraded
        selections only).
    ``opens``
        turns where the model actually opened its document. This is the usage
        signal; ``offers`` alone cannot distinguish "never chosen" from "chosen
        and useless".
    ``follow_through``
        of the opens, the fraction where the subsequent tool calls stayed
        inside what the skill declared.
    ``success_rate``
        of the opens, the fraction of turns that succeeded.
    ``uplift``
        success with the skill exposed minus success without it, where a paired
        comparison exists. ``None`` when it does not.
    ``exposure_days``
        how long it has been exposed at all.

    A library that only grows fills with near-duplicates and degrades selection
    precision for everything in it — but every judgement here is a *proposal*,
    because removing a capability produces no error, only a slow decline.
    """
    out: List[Dict[str, Any]] = []
    for asset_id, stats in (usage or {}).items():
        offers = int(stats.get("offers", 0) or 0)
        opens = int(stats.get("opens", 0) or 0)
        exposure_days = float(stats.get("exposure_days", 0) or 0)
        uplift = stats.get("uplift")

        if uplift is not None and float(uplift) <= -0.05:
            out.append(
                {
                    "asset_id": asset_id,
                    "reason": promotion_constants.REASON_HARMFUL,
                    "action": promotion_constants.ACTION_ROLLBACK,
                    "explanation": (
                        f"成对比较显示有它反而更差（{float(uplift):+.0%}）——"
                        "这是唯一需要立即停止曝光的情况"
                    ),
                    **stats,
                }
            )
            continue

        if exposure_days < promotion_constants.MIN_EXPOSURE_DAYS:
            # Too new to judge. Saying nothing is the honest output; the old
            # rule's answer here was "retire it", which punished every skill for
            # having just shipped.
            continue

        if opens == 0:
            if offers < promotion_constants.MIN_OFFERS_BEFORE_JUDGING:
                continue
            out.append(
                {
                    "asset_id": asset_id,
                    "reason": promotion_constants.REASON_NEVER_OPENED,
                    "action": promotion_constants.ACTION_LOWER_EXPOSURE,
                    "explanation": (
                        f"已被挂载 {offers} 次、"
                        # A hand-authored skill has no release record and so no
                        # finite exposure window — it has been there from the
                        # start. Formatting that as a number printed "曝光 inf 天".
                        + (
                            "一直可用"
                            if exposure_days == float("inf")
                            else f"曝光 {exposure_days:.0f} 天"
                        )
                        + "，模型一次都没打开——"
                        "多半是描述没写清它解决什么问题，而不是能力本身没用"
                    ),
                    **stats,
                }
            )
            continue

        if opens < promotion_constants.MIN_OPENS_BEFORE_JUDGING:
            continue

        follow_through = stats.get("follow_through")
        if follow_through is not None and float(follow_through) < promotion_constants.FOLLOW_THROUGH_FLOOR:
            out.append(
                {
                    "asset_id": asset_id,
                    "reason": promotion_constants.REASON_IGNORED_WHEN_OPENED,
                    "action": promotion_constants.ACTION_REWRITE,
                    "explanation": (
                        f"被打开 {opens} 次，但只有 {float(follow_through):.0%} 的情况"
                        "后续动作真的照它说的做——文档本身需要重写，不是该退役"
                    ),
                    **stats,
                }
            )
            continue

        success_rate = float(stats.get("success_rate", 0.0) or 0.0)
        if success_rate < promotion_constants.SUCCESS_FLOOR:
            out.append(
                {
                    "asset_id": asset_id,
                    "reason": promotion_constants.REASON_LOW_SUCCESS,
                    "action": promotion_constants.ACTION_RETIRE,
                    "explanation": (
                        f"被打开 {opens} 次、照做了，但成功率只有 {success_rate:.0%}"
                    ),
                    **stats,
                }
            )
    return out


def detect_negative_transfer(
    *,
    asset_id: str,
    related_task_types: Sequence[str],
    before: Dict[str, float],
    after: Dict[str, float],
    threshold: float = 0.05,
) -> List[promotion_types.NegativeTransfer]:
    """Find task types that got worse after an activation *and are unrelated to it*.

    Only unrelated tasks are examined. A dip on the task the change targeted is
    a straightforward regression its own guardrail catches; the dangerous case
    is collateral damage elsewhere, which no single release's guardrail is
    watching and which accumulates silently across many individually-harmless
    changes.
    """
    related = set(related_task_types)
    findings: List[promotion_types.NegativeTransfer] = []
    for task_type, after_value in (after or {}).items():
        if task_type in related:
            continue
        before_value = (before or {}).get(task_type)
        if before_value is None:
            continue
        if before_value - after_value >= threshold:
            findings.append(
                promotion_types.NegativeTransfer(
                    asset_id=asset_id,
                    task_type=task_type,
                    before=before_value,
                    after=after_value,
                )
            )
    return findings
