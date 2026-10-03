"""Agent assembly: prompt policy. """

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
from core.llm.factory.defaults import DYNAMIC_BLOCK_HEADER


def _render_turn_budget_hint(max_iters: int) -> str:
    """Tell an agent that *does* have a turn budget how to spend it.

    Stating the budget up front is cheaper than shouting about it at the end:
    the wrap-up reminder (:class:`IterBudgetReminderMiddleware`) can only ask a
    loop that already burned its rounds one tool at a time to salvage
    something, while this changes how the rounds get spent in the first place.
    Parallel fan-out within one round is the lever — a round is one reasoning
    step, not one tool call, so the budget binds serial round-trips, never the
    total number of tool calls.

    Only rendered where a bound actually exists (sub-agents, turbo, custom
    agents, an explicit operator cap). The main agent is unbounded and must not
    be told otherwise — a false scarcity claim would make it cut work short.
    """
    return (
        "\n\n## 轮次预算\n"
        f"本次运行最多 {max_iters} 轮「推理 → 工具调用」。一轮里可以同时发起任意多个"
        "工具调用，所以受限的是串行往返次数，不是工具调用总数。\n"
        "- 先想清楚需要哪些信息，再**在同一轮里并行发起**全部彼此独立的调用"
        "（一次读多个文件、一次检索多个关键词），不要一轮只调一个。\n"
        "- 只有后一步的参数确实要等前一步的结果时，才分成下一轮。\n"
        "- 同一个操作连续失败两次就换思路或如实报告，不要用剩余轮次反复重试。\n"
    )


def _render_dynamic_block(fragments: list[str]) -> str:
    """Render evolved prompt fragments as one clearly-attributed section."""
    lines = [DYNAMIC_BLOCK_HEADER, ""]
    lines.extend(fragment.strip() for fragment in fragments if fragment.strip())
    return "\n\n".join(lines)


def _resolve_prompt_fragments(fragment_ids: list[str]) -> list[str]:
    """The text of each fragment a profile references.

    Fragments are stored as prompt candidates and referenced by id, not copied
    into the profile. Copying would mean a fragment corrected in one place stays
    wrong everywhere it was pasted, and there would be no single row to roll
    back. A reference that no longer resolves is skipped rather than rendered as
    an empty line, so a retired fragment leaves no trace in the prompt.
    """
    if not fragment_ids:
        return []
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionCandidate

        with SessionLocal() as db:
            rows = (
                db.query(EvolutionCandidate)
                .filter(
                    EvolutionCandidate.target_kind == "prompt",
                    EvolutionCandidate.target_asset_id.in_(list(fragment_ids)),
                    EvolutionCandidate.status == "active",
                )
                .all()
            )
        by_id = {
            str(row.target_asset_id): str(
                ((row.ir or {}).get("changes") or [{}])[0].get("fragment") or ""
            )
            for row in rows
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[factory] prompt fragments unavailable: %s", exc)
        return []
    return [by_id[fid] for fid in fragment_ids if by_id.get(fid)]
