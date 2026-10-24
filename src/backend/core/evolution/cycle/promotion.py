"""Promote recurring sequences, procedures and skill combinations into proposals."""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging
from core.evolution.cycle import patterns
from core.evolution.cycle import promotion_constants
from core.evolution.cycle import promotion_types

logger = logging.getLogger(__name__)

def dominant_shared_intent(
    pattern: patterns.Pattern,
    episodes: Optional[Sequence[Dict[str, Any]]],
    *,
    support_floor: int,
):
    """The one intent behind a sequence's supporting episodes, or ``None``.

    ``None`` is a refusal, and deliberately covers the honest-uncertainty cases
    too: no views supplied, request text too short to cluster, no clustering
    method available. "We cannot tell whether these conversations had anything
    in common" and "they had nothing in common" both mean no SOP claim can be
    made — treating the first as a pass is how single-tool coincidences used to
    become skills.
    """
    if not episodes:
        return None
    wanted = {str(e) for e in pattern.episode_ids}
    supporting = [e for e in episodes if str(e.get("episode_id") or "") in wanted]
    if len(supporting) < support_floor:
        return None

    from core.evolution.assessment.similarity import METHOD_NONE, cluster_intents

    clusters, method = cluster_intents(supporting, min_support=2)
    if method == METHOD_NONE or not clusters:
        return None
    top = clusters[0]
    if len(top.episode_ids) < support_floor:
        return None
    if len(top.episode_ids) / len(supporting) < promotion_constants.INTENT_COHESION_RATIO:
        return None
    # 复现必须跨会话：occasions 按 chat_id 去重，同一会话里反复跑同一套
    # 流程只算一次场合，不构成"这件事总在发生"。
    if top.occasions < support_floor:
        return None
    return top


def promote_tool_sequence_to_skill(
    pattern: patterns.Pattern,
    *,
    episodes: Optional[Sequence[Dict[str, Any]]] = None,
    min_support: Optional[int] = None,
    skill_credit: float = 0.0,
    workflow_credit: float = 0.0,
    existing_skill_signatures: Optional[Dict[str, str]] = None,
) -> Optional[promotion_types.PromotionProposal]:
    """Compile a recurring successful tool sequence into a skill proposal.

    A recurring sequence alone is *not* evidence of a reusable capability.
    Generic tools recur across unrelated conversations by construction, and the
    limiting case — a whole conversation whose "sequence" is one call to
    ``view_text_file`` — says only that the task was trivial. What justifies an
    SOP is commonality of *purpose*: the same kind of request, on separate
    occasions, repeatedly completed by the same non-trivial procedure. So the
    gates hold together, and each excludes a specific way of being wrong:

    * the sequence is **non-trivial** (≥ :data:`MIN_TOOL_SEQUENCE_LEN` steps) —
      wrapping one generic tool adds an asset and no knowledge;
    * the supporting episodes **share an intent** — a dominant cluster over
      their request text (via :mod:`core.evolution.assessment.similarity`) covers
      ≥ :data:`INTENT_COHESION_RATIO` of them; without that the recurrence is a
      coincidence of tooling, and no commonality means no promotion;
    * the recurrence spans **separate conversations** — one chat re-running the
      same procedure is one occasion, not evidence it keeps coming back;
    * the recurrences **succeed** at ≥ :data:`SKILL_MIN_SUCCESS_RATE`.

    ``episodes`` are the flattened views the pattern was discovered from; the
    intent gate reads their request text. A caller that cannot supply them gets
    a refusal, not the benefit of the doubt.

    Named for what it reads. This was called ``promote_memory_to_skill`` and
    carried ``from_layer="memory"``, but no memory content ever enters the
    result — ``pattern.memory_refs`` are hashes of whatever happened to be
    retrieved during the supporting episodes. The name promised a memory→skill
    chain the code did not have; :func:`promote_procedural_memory_to_skill` is
    that chain.

    Refuses when orchestration is the better explanation: a stable tool ordering
    can equally mean "the workflow is fixed" rather than "there is a reusable
    skill here". Promoting in that case creates a skill with no independent
    content, which then has to be maintained forever.
    """
    if pattern.kind != promotion_constants.PATTERN_SUCCESS_SUBSEQUENCE or not pattern.tool_sequence:
        return None
    support_floor = promotion_constants.MIN_SUPPORT if min_support is None else int(min_support)
    if len(pattern.tool_sequence) < promotion_constants.MIN_TOOL_SEQUENCE_LEN:
        logger.info(
            "[promotion] skipping %s: %d-step sequence is below the SOP floor",
            pattern.signature,
            len(pattern.tool_sequence),
        )
        return None
    if pattern.support < support_floor:
        return None
    if pattern.success_rate < promotion_constants.SKILL_MIN_SUCCESS_RATE:
        return None
    if workflow_credit > skill_credit:
        logger.info(
            "[promotion] skipping skill promotion for %s: workflow credit higher",
            pattern.signature,
        )
        return None

    intent = dominant_shared_intent(pattern, episodes, support_floor=support_floor)
    if intent is None:
        logger.info(
            "[promotion] skipping %s: supporting episodes share no intent — a "
            "recurring sequence without a recurring request is not an SOP",
            pattern.signature,
        )
        return None
    covered = [e for e in pattern.episode_ids if e in set(intent.episode_ids)]

    existing = (existing_skill_signatures or {}).get(pattern.signature)
    return promotion_types.PromotionProposal(
        from_layer="episode",
        to_layer="skill",
        signature=pattern.signature,
        support=len(covered),
        rationale=(
            f"「{intent.representative[:40]}」类请求在 {intent.occasions} 次独立会话中"
            f"反复出现，每次都以同一 {len(pattern.tool_sequence)} 步工具序列成功完成"
            f"（{len(covered)} 个 Episode，成功率 {pattern.success_rate:.0%}）——"
            "同类意图 + 稳定做法，具备沉淀为 SOP 的共性"
        ),
        # Co-retrieved memories are *context*, not sources. They are carried so
        # a reviewer can see what was in view, and deliberately not demoted —
        # nothing of theirs went into this skill, so down-weighting them would
        # remove knowledge the skill does not carry.
        source_refs=[],
        payload={
            "tool_sequence": pattern.tool_sequence,
            "episode_ids": covered[:20],
            "intent": intent.representative,
            "occasions": intent.occasions,
            "co_retrieved_memory_refs": list(pattern.memory_refs),
        },
        # An equivalent skill already exists → patch it instead of adding a twin.
        patch_target=existing,
    )


def promote_procedural_memory_to_skill(
    *,
    memories: Sequence[Dict[str, Any]],
    intent_cluster: Dict[str, Any],
    occurrences: int,
    success_rate: float,
    plan_variety: int,
    existing_skill_id: Optional[str] = None,
) -> Optional[promotion_types.PromotionProposal]:
    """Compile *how work is done here* into a skill proposal.

    This is the chain the design described and the code never had. Four
    conditions must hold together, and each excludes a specific way of being
    wrong:

    * the memories are **procedural** — a fact frozen into a document stops
      being current, and a preference is already covered by one profile line;
    * the intent recurs across **separate occasions** — one conversation
      mentioning something three times is one occasion;
    * the recurrences **succeed** — distilling a failing approach makes it the
      house style;
    * the plans **varied** — the same request handled identically every time is
      already effectively a skill, and writing one down adds an asset to
      maintain while changing nothing.

    The proposal carries the memories' **content**, which is what makes this a
    compilation rather than a rename, and their refs, which is what lets the
    source memories be demoted when the skill goes live and restored when it is
    rolled back.
    """
    procedural = [
        m
        for m in memories
        if str(m.get("memory_type") or "") == "procedural" and str(m.get("content") or "").strip()
    ]
    if not procedural:
        return None
    if occurrences < promotion_constants.PROCEDURAL_MIN_OCCURRENCES:
        return None
    if success_rate < promotion_constants.PROCEDURAL_MIN_SUCCESS_RATE:
        return None
    if plan_variety < 2:
        return None

    representative = str(intent_cluster.get("representative") or "")
    return promotion_types.PromotionProposal(
        from_layer="memory",
        to_layer="skill",
        signature="proc:" + "|".join(sorted(str(m.get("ref") or "") for m in procedural)),
        support=occurrences,
        rationale=(
            f"「{representative[:40]}」类请求出现 {occurrences} 次、成功率 "
            f"{success_rate:.0%}，却用了 {plan_variety} 种不同做法；"
            f"其中 {len(procedural)} 条做法/口径已被反复检索，应编译为一次性装配的技能"
        ),
        source_refs=[str(m.get("ref") or "") for m in procedural if m.get("ref")],
        payload={
            "procedures": [
                {
                    "rule": str(m.get("content") or ""),
                    "why": str(m.get("why") or ""),
                    "applies_to": str(m.get("applies_to") or ""),
                }
                for m in procedural
            ],
            "episode_ids": [str(e) for e in (intent_cluster.get("episode_ids") or [])][:20],
            "intent": representative,
            "memory_refs": [str(m.get("ref") or "") for m in procedural if m.get("ref")],
        },
        patch_target=existing_skill_id,
    )


def promote_skills_to_orchestration(
    pattern: patterns.Pattern,
    *,
    combo_success_rate: float,
    individual_success_rate: float,
    task_types: Optional[Sequence[str]] = None,
    skill_versions: Optional[Dict[str, str]] = None,
) -> Optional[promotion_types.PromotionProposal]:
    """Promote a recurring skill combination into an **agent profile** proposal.

    Promoting it into a *workflow* — a fixed DAG — was the wrong target: the
    main axis is a ReAct loop, which never executes a DAG, so a workflow asset
    could only ever govern the separate autonomous-loop product. What a
    recurring skill combination actually says is "for this kind of task, these
    capabilities belong in view together", and the thing that decides what is in
    view is the profile.

    The combination must *beat its parts*: frequency alone would promote every
    common sequence regardless of whether grouping them helps. The proposal is
    declarative — a skill set and the task types it applies to — and carries no
    executable content.
    """
    if not pattern.skill_sequence or len(pattern.skill_sequence) < 2:
        return None
    if pattern.support < promotion_constants.WORKFLOW_MIN_COMBO_FREQUENCY:
        return None
    if combo_success_rate - individual_success_rate < promotion_constants.WORKFLOW_MIN_UPLIFT:
        return None

    versions = skill_versions or {}
    return promotion_types.PromotionProposal(
        from_layer="skill",
        to_layer="agent_profile",
        signature=pattern.signature,
        support=pattern.support,
        rationale=(
            f"技能组合出现 {pattern.support} 次，组合成功率 {combo_success_rate:.0%} "
            f"显著高于零散调用 {individual_success_rate:.0%}——"
            "应固化为该类任务的技能组合，而不是每次重新挑选"
        ),
        source_refs=list(pattern.skill_sequence),
        payload={
            "skill_ids": list(dict.fromkeys(pattern.skill_sequence)),
            "task_types": [str(t) for t in (task_types or [])],
            # Declared so the candidate auto-invalidates if a dependency retires
            # — a profile pointing at a dead skill version is worse than none.
            "depends_on": {s: versions.get(s, "") for s in pattern.skill_sequence},
            "kind": "declarative_policy",
        },
    )


def dependencies_satisfied(
    proposal: promotion_types.PromotionProposal, live_skill_versions: Dict[str, str]
) -> Tuple[bool, List[str]]:
    """Whether every declared dependency still exists at the declared version.

    This is the mechanism that makes cross-layer rollback work in one direction:
    retire a skill and every profile built on it stops validating, instead of
    quietly pointing at something that is no longer there.
    """
    declared = (proposal.payload or {}).get("depends_on") or {}
    stale = [
        skill_id
        for skill_id, version in declared.items()
        if version and live_skill_versions.get(skill_id) != version
    ]
    return (not stale), stale


def find_overlaps(
    signatures: Dict[str, List[str]], *, min_shared: int = 2
) -> List[Tuple[str, str, int]]:
    """Assets whose **declared** step sequences overlap enough to be merge candidates.

    ``signatures`` must be each asset's own declared tools — the tools its
    document names. Feeding it the tools observed in episodes where the asset
    was merely loaded produces a merge proposal for every pair of skills that
    happened to be enabled together, which is guaranteed rather than
    informative: co-enabled skills share the episode's whole tool set by
    construction.
    """
    overlaps: List[Tuple[str, str, int]] = []
    asset_ids = sorted(signatures)
    for i, a in enumerate(asset_ids):
        for b in asset_ids[i + 1 :]:
            shared = len(set(signatures[a]) & set(signatures[b]))
            if shared >= min_shared:
                overlaps.append((a, b, shared))
    return overlaps
