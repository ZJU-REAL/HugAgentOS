"""Learn tool orderings that hold across successful episodes and apply them."""
from __future__ import annotations
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from core.evolution.cycle import patterns
from core.evolution.cycle import promotion_constants
from core.evolution.cycle import promotion_types

def extract_ordering_constraints(
    episodes: Sequence[Dict[str, Any]],
    *,
    min_support: int = promotion_constants.MIN_SUPPORT,
    min_contexts: int = promotion_constants.MIN_CONSTRAINT_CONTEXTS,
) -> List[promotion_types.OrderingConstraint]:
    """Find orderings that hold *invariantly* across successful episodes.

    Conflicting evidence vetoes a constraint outright rather than letting the
    majority win. A pair that appears both ways round is not a rule — it is a
    signal that ordering depends on context the system cannot yet see, and
    promoting the popular direction would actively break the minority case.
    """
    forward: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    by_task_type: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    for episode in episodes:
        if episode.get("verdict") != "success":
            continue
        tools = [str(t) for t in (episode.get("tool_sequence") or [])]
        if len(tools) < 2:
            continue
        shape = patterns.pattern_signature(sorted(set(tools)))
        task_type = str(episode.get("task_type") or "unknown")
        for i, earlier in enumerate(tools):
            for later in tools[i + 1 :]:
                if earlier == later:
                    continue
                forward[(earlier, later)].add(shape)
                by_task_type[(earlier, later)].add(task_type)

    constraints: List[promotion_types.OrderingConstraint] = []
    for (a, b), shapes in forward.items():
        validated = by_task_type[(a, b)]
        contradicted = by_task_type.get((b, a), set())

        # A pair that flips *within the same task type* is genuinely ambiguous
        # there — no amount of context resolves it, so the rule is dropped for
        # that type rather than resolved by majority.
        ambiguous = validated & contradicted
        validated = validated - ambiguous
        if not validated:
            continue

        support = sum(
            1
            for e in episodes
            if e.get("verdict") == "success"
            and a in (e.get("tool_sequence") or [])
            and b in (e.get("tool_sequence") or [])
            and str(e.get("task_type") or "unknown") in validated
        )
        if support < min_support or len(shapes) < min_contexts:
            continue
        constraints.append(
            promotion_types.OrderingConstraint(
                before=a,
                after=b,
                support=support,
                contexts=len(shapes),
                validated_in=tuple(sorted(validated)),
                contradicted_in=tuple(sorted(contradicted | ambiguous)),
            )
        )

    constraints.sort(key=lambda c: (c.contexts, c.support), reverse=True)
    return constraints


def apply_ordering_constraints(
    tools: Sequence[str],
    constraints: Sequence[promotion_types.OrderingConstraint],
    task_type: Optional[str] = None,
) -> List[str]:
    """Reorder a task's tools to satisfy every applicable constraint.

    A stable topological sort: tools with no constraint between them keep their
    original relative order, so applying a rule about one pair never silently
    reshuffles everything else.
    """
    items = list(tools)
    applicable = [c for c in constraints if c.applies_to(items, task_type)]
    if not applicable:
        return items

    for _ in range(len(items)):
        changed = False
        for constraint in applicable:
            i, j = items.index(constraint.before), items.index(constraint.after)
            if i > j:
                items.insert(j, items.pop(i))
                changed = True
        if not changed:
            break
    return items
