"""Cross-engine capability promotion (GCE tickets 32 / 33 / 34 / 36).

This is the part that makes the system *learn* rather than merely record.
Capability flows up a chain:

    raw traces → episodic memory → skill candidate → workflow candidate → domain
    constraint → released asset

The idea is that experience should not stay in whichever form it first arrived
in.  A pattern that keeps recurring as scattered memories is re-retrieved and
re-planned on every occurrence; compiled into a skill, it is assembled once.  A
combination of skills that keeps appearing in the same order is re-derived every
time; promoted to a workflow, it becomes reusable — which is the direct answer
to "orchestration never accumulates", the largest gap in the current system.

Two design commitments:

* **Promotion, not duplication.**  A promoted memory is down-weighted rather
  than deleted, and the link is bidirectional, so "why does this skill exist?"
  has an answer and a rollback can restore the original weighting.
* **Aggregate evidence by default.**  A single high-confidence failure may
  trigger a candidate, but the normal entry point is a recurring cluster.
  Otherwise the system permanently encodes noise.

Flow also runs downhill: overlapping assets merge, unused ones retire, and
**negative transfer** — a change that quietly degrades tasks it has nothing to do
with — is detected. That last one is the most dangerous failure mode of joint
evolution and the easiest to miss, because no single release's guardrail ever
trips.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Pattern families discovered from episode clusters.
PATTERN_REPEATED_FAILURE = "repeated_failure"
PATTERN_SUCCESS_SUBSEQUENCE = "success_subsequence"
PATTERN_DRIFT = "execution_drift"

# Minimum cluster size before anything downstream is produced. A lower bar would
# let one-off noise become a permanent asset.
MIN_SUPPORT = 5
# A single failure may still trigger, but is flagged so reviewers can see that
# it rests on one observation.
SINGLE_SHOT_CONFIDENCE = 0.85

# Promotion thresholds.
SKILL_MIN_SUCCESS_RATE = 0.7
WORKFLOW_MIN_COMBO_FREQUENCY = 5
# A combination must beat its parts, not merely be frequent — otherwise every
# common sequence becomes a workflow whether or not the grouping helps.
WORKFLOW_MIN_UPLIFT = 0.05

# 一段 SOP 至少要有的步骤数。长度 1–2 的"序列"几乎总是通用工具的巧合复现——
# 整轮对话只调一次 view_text_file 也会形成"相同序列"——包装它们不携带任何
# 过程知识，只往评审队列里添噪声。
MIN_TOOL_SEQUENCE_LEN = 3
# 支撑 Episode 中归入同一意图簇的最低占比。序列相同只说明工具巧合，
# 意图相同才说明存在可复用的做事方法——这是把一段序列当 SOP 的前提。
INTENT_COHESION_RATIO = 0.6


@dataclass
class Pattern:
    """A recurring shape found across episodes."""

    kind: str
    signature: str
    support: int
    episode_ids: List[str] = field(default_factory=list)
    success_rate: float = 0.0
    tool_sequence: List[str] = field(default_factory=list)
    skill_sequence: List[str] = field(default_factory=list)
    memory_refs: List[str] = field(default_factory=list)
    single_shot: bool = False

    @property
    def meets_support(self) -> bool:
        return self.support >= MIN_SUPPORT or self.single_shot

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "signature": self.signature,
            "support": self.support,
            "success_rate": round(self.success_rate, 3),
            "tool_sequence": self.tool_sequence,
            "skill_sequence": self.skill_sequence,
            "memory_refs": self.memory_refs,
            "single_shot": self.single_shot,
            "episode_ids": self.episode_ids[:20],
        }


def _signature(items: Sequence[str]) -> str:
    return "→".join(items)


def discover_patterns(episodes: List[Dict[str, Any]]) -> List[Pattern]:
    """Cluster episodes into recurring shapes.

    ``episodes`` are plain dicts so this stays testable without a database and
    usable over backfilled history (which has no asset bundle and therefore
    cannot be replayed, but is perfectly good for finding patterns).
    """
    by_tool_seq: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_skill_seq: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for episode in episodes:
        tools = [str(t) for t in (episode.get("tool_sequence") or [])]
        skills = [str(s) for s in (episode.get("skill_sequence") or [])]
        if tools:
            by_tool_seq[_signature(tools)].append(episode)
        if len(skills) >= 2:
            by_skill_seq[_signature(skills)].append(episode)

    patterns: List[Pattern] = []

    for signature, group in by_tool_seq.items():
        successes = [e for e in group if e.get("verdict") == "success"]
        failures = [e for e in group if e.get("verdict") == "failed"]
        rate = len(successes) / len(group) if group else 0.0

        if len(successes) >= MIN_SUPPORT and rate >= SKILL_MIN_SUCCESS_RATE:
            patterns.append(
                Pattern(
                    kind=PATTERN_SUCCESS_SUBSEQUENCE,
                    signature=signature,
                    support=len(successes),
                    episode_ids=[e.get("episode_id", "") for e in successes],
                    success_rate=rate,
                    tool_sequence=signature.split("→"),
                    memory_refs=sorted(
                        {
                            ref
                            for e in successes
                            for ref in (e.get("memory_refs") or [])
                        }
                    ),
                )
            )
        if len(failures) >= MIN_SUPPORT:
            patterns.append(
                Pattern(
                    kind=PATTERN_REPEATED_FAILURE,
                    signature=signature,
                    support=len(failures),
                    episode_ids=[e.get("episode_id", "") for e in failures],
                    success_rate=rate,
                    tool_sequence=signature.split("→"),
                )
            )

    for signature, group in by_skill_seq.items():
        if len(group) >= WORKFLOW_MIN_COMBO_FREQUENCY:
            successes = [e for e in group if e.get("verdict") == "success"]
            patterns.append(
                Pattern(
                    kind=PATTERN_SUCCESS_SUBSEQUENCE,
                    signature=signature,
                    support=len(group),
                    episode_ids=[e.get("episode_id", "") for e in group],
                    success_rate=len(successes) / len(group),
                    skill_sequence=signature.split("→"),
                )
            )

    return [p for p in patterns if p.meets_support]


# ── Memory → Skill ───────────────────────────────────────────────────────────


@dataclass
class PromotionProposal:
    """A promotion the chain wants to make, before it becomes an IR candidate."""

    from_layer: str
    to_layer: str
    signature: str
    support: int
    rationale: str
    source_refs: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    # Set when an equivalent asset already exists: promotion becomes a patch
    # rather than a new asset, so the library does not fill with near-duplicates.
    patch_target: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_layer": self.from_layer,
            "to_layer": self.to_layer,
            "signature": self.signature,
            "support": self.support,
            "rationale": self.rationale,
            "source_refs": list(self.source_refs),
            "payload": self.payload,
            "patch_target": self.patch_target,
        }


def _dominant_shared_intent(
    pattern: Pattern,
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

    from core.evolution.similarity import METHOD_NONE, cluster_intents

    clusters, method = cluster_intents(supporting, min_support=2)
    if method == METHOD_NONE or not clusters:
        return None
    top = clusters[0]
    if len(top.episode_ids) < support_floor:
        return None
    if len(top.episode_ids) / len(supporting) < INTENT_COHESION_RATIO:
        return None
    # 复现必须跨会话：occasions 按 chat_id 去重，同一会话里反复跑同一套
    # 流程只算一次场合，不构成"这件事总在发生"。
    if top.occasions < support_floor:
        return None
    return top


def promote_tool_sequence_to_skill(
    pattern: Pattern,
    *,
    episodes: Optional[Sequence[Dict[str, Any]]] = None,
    min_support: Optional[int] = None,
    skill_credit: float = 0.0,
    workflow_credit: float = 0.0,
    existing_skill_signatures: Optional[Dict[str, str]] = None,
) -> Optional[PromotionProposal]:
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
      their request text (via :mod:`core.evolution.similarity`) covers
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
    if pattern.kind != PATTERN_SUCCESS_SUBSEQUENCE or not pattern.tool_sequence:
        return None
    support_floor = MIN_SUPPORT if min_support is None else int(min_support)
    if len(pattern.tool_sequence) < MIN_TOOL_SEQUENCE_LEN:
        logger.info(
            "[promotion] skipping %s: %d-step sequence is below the SOP floor",
            pattern.signature,
            len(pattern.tool_sequence),
        )
        return None
    if pattern.support < support_floor:
        return None
    if pattern.success_rate < SKILL_MIN_SUCCESS_RATE:
        return None
    if workflow_credit > skill_credit:
        logger.info(
            "[promotion] skipping skill promotion for %s: workflow credit higher",
            pattern.signature,
        )
        return None

    intent = _dominant_shared_intent(pattern, episodes, support_floor=support_floor)
    if intent is None:
        logger.info(
            "[promotion] skipping %s: supporting episodes share no intent — a "
            "recurring sequence without a recurring request is not an SOP",
            pattern.signature,
        )
        return None
    covered = [e for e in pattern.episode_ids if e in set(intent.episode_ids)]

    existing = (existing_skill_signatures or {}).get(pattern.signature)
    return PromotionProposal(
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


# Enough separate occasions that "this keeps coming up" is a claim about
# behaviour rather than about one busy afternoon.
PROCEDURAL_MIN_OCCURRENCES = 3
# Below this the procedure is being followed into failures, and compiling it
# would encode the failure as the house style.
PROCEDURAL_MIN_SUCCESS_RATE = 0.6


def promote_procedural_memory_to_skill(
    *,
    memories: Sequence[Dict[str, Any]],
    intent_cluster: Dict[str, Any],
    occurrences: int,
    success_rate: float,
    plan_variety: int,
    existing_skill_id: Optional[str] = None,
) -> Optional[PromotionProposal]:
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
    if occurrences < PROCEDURAL_MIN_OCCURRENCES:
        return None
    if success_rate < PROCEDURAL_MIN_SUCCESS_RATE:
        return None
    if plan_variety < 2:
        return None

    representative = str(intent_cluster.get("representative") or "")
    return PromotionProposal(
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


# Down-weighting the memories a skill was compiled from lives in
# :mod:`core.evolution.memory_apply`, because it is an applied change with an
# undo rather than a list of dictionaries. The version that used to sit here
# returned a plan nothing executed, and its ``restore_on_rollback: True`` was a
# promise no code kept.


# ── Skill → Orchestration ────────────────────────────────────────────────────


def promote_skills_to_orchestration(
    pattern: Pattern,
    *,
    combo_success_rate: float,
    individual_success_rate: float,
    task_types: Optional[Sequence[str]] = None,
    skill_versions: Optional[Dict[str, str]] = None,
) -> Optional[PromotionProposal]:
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
    if pattern.support < WORKFLOW_MIN_COMBO_FREQUENCY:
        return None
    if combo_success_rate - individual_success_rate < WORKFLOW_MIN_UPLIFT:
        return None

    versions = skill_versions or {}
    return PromotionProposal(
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
    proposal: PromotionProposal, live_skill_versions: Dict[str, str]
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


# ── Downhill flow: overlap, retirement, negative transfer ────────────────────


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


# How long a skill must have been exposed before "never opened" means anything.
# Retiring on a short window measures release recency, not usefulness — which
# is exactly what the old selection-count rule did, since every enabled skill
# recorded the same count and the only real variable was how recently a skill
# had shipped.
MIN_EXPOSURE_DAYS = 14
# Offered this many times without ever being opened → the model consistently
# decides it is not relevant.
MIN_OFFERS_BEFORE_JUDGING = 10
# Opened this many times before follow-through is worth judging.
MIN_OPENS_BEFORE_JUDGING = 3
# Opened, but the plan afterwards ignored what it said.
FOLLOW_THROUGH_FLOOR = 0.4
SUCCESS_FLOOR = 0.4

REASON_NEVER_OPENED = "never_opened"
REASON_IGNORED_WHEN_OPENED = "ignored_when_opened"
REASON_LOW_SUCCESS = "low_success_when_opened"
REASON_HARMFUL = "worse_than_without"

# What to do about it. Distinct because the fixes are distinct: a skill nobody
# opens has a discovery problem (its description does not match the requests it
# serves), a skill that gets opened and ignored has a *content* problem, and a
# skill that makes things worse has to stop being exposed now.
ACTION_LOWER_EXPOSURE = "lower_exposure"
ACTION_REWRITE = "rewrite"
ACTION_RETIRE = "retire"
ACTION_ROLLBACK = "rollback"


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
                    "reason": REASON_HARMFUL,
                    "action": ACTION_ROLLBACK,
                    "explanation": (
                        f"成对比较显示有它反而更差（{float(uplift):+.0%}）——"
                        "这是唯一需要立即停止曝光的情况"
                    ),
                    **stats,
                }
            )
            continue

        if exposure_days < MIN_EXPOSURE_DAYS:
            # Too new to judge. Saying nothing is the honest output; the old
            # rule's answer here was "retire it", which punished every skill for
            # having just shipped.
            continue

        if opens == 0:
            if offers < MIN_OFFERS_BEFORE_JUDGING:
                continue
            out.append(
                {
                    "asset_id": asset_id,
                    "reason": REASON_NEVER_OPENED,
                    "action": ACTION_LOWER_EXPOSURE,
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

        if opens < MIN_OPENS_BEFORE_JUDGING:
            continue

        follow_through = stats.get("follow_through")
        if follow_through is not None and float(follow_through) < FOLLOW_THROUGH_FLOOR:
            out.append(
                {
                    "asset_id": asset_id,
                    "reason": REASON_IGNORED_WHEN_OPENED,
                    "action": ACTION_REWRITE,
                    "explanation": (
                        f"被打开 {opens} 次，但只有 {float(follow_through):.0%} 的情况"
                        "后续动作真的照它说的做——文档本身需要重写，不是该退役"
                    ),
                    **stats,
                }
            )
            continue

        success_rate = float(stats.get("success_rate", 0.0) or 0.0)
        if success_rate < SUCCESS_FLOOR:
            out.append(
                {
                    "asset_id": asset_id,
                    "reason": REASON_LOW_SUCCESS,
                    "action": ACTION_RETIRE,
                    "explanation": (
                        f"被打开 {opens} 次、照做了，但成功率只有 {success_rate:.0%}"
                    ),
                    **stats,
                }
            )
    return out


@dataclass
class NegativeTransfer:
    asset_id: str
    task_type: str
    before: float
    after: float

    @property
    def delta(self) -> float:
        return self.after - self.before

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "task_type": self.task_type,
            "before": round(self.before, 4),
            "after": round(self.after, 4),
            "delta": round(self.delta, 4),
        }


def detect_negative_transfer(
    *,
    asset_id: str,
    related_task_types: Sequence[str],
    before: Dict[str, float],
    after: Dict[str, float],
    threshold: float = 0.05,
) -> List[NegativeTransfer]:
    """Find task types that got worse after an activation *and are unrelated to it*.

    Only unrelated tasks are examined. A dip on the task the change targeted is
    a straightforward regression its own guardrail catches; the dangerous case
    is collateral damage elsewhere, which no single release's guardrail is
    watching and which accumulates silently across many individually-harmless
    changes.
    """
    related = set(related_task_types)
    findings: List[NegativeTransfer] = []
    for task_type, after_value in (after or {}).items():
        if task_type in related:
            continue
        before_value = (before or {}).get(task_type)
        if before_value is None:
            continue
        if before_value - after_value >= threshold:
            findings.append(
                NegativeTransfer(
                    asset_id=asset_id,
                    task_type=task_type,
                    before=before_value,
                    after=after_value,
                )
            )
    return findings


# ── Transferable ordering constraints ────────────────────────────────────────


@dataclass
class OrderingConstraint:
    """A pairwise ordering that held across the supporting evidence.

    Carries the task types it was *validated in* and any it was *contradicted
    in*. That distinction is what lets a genuinely context-dependent ordering be
    used where it holds instead of being discarded everywhere.
    """

    before: str
    after: str
    support: int
    contexts: int  # distinct tool-set shapes it was observed in
    validated_in: Tuple[str, ...] = ()
    contradicted_in: Tuple[str, ...] = ()

    def applies_to(self, tools: Sequence[str], task_type: Optional[str] = None) -> bool:
        if self.before not in tools or self.after not in tools:
            return False
        if task_type is None:
            # No context given: only a rule contradicted nowhere may be used.
            return not self.contradicted_in
        if task_type in self.contradicted_in:
            return False
        if task_type in self.validated_in:
            return True
        # An unseen context. Extending there is only safe for a rule that has
        # never been contradicted; one known to flip somewhere is precisely the
        # kind that should not be guessed at.
        return not self.contradicted_in

    def to_dict(self) -> Dict[str, Any]:
        return {
            "before": self.before,
            "after": self.after,
            "support": self.support,
            "contexts": self.contexts,
            "validated_in": list(self.validated_in),
            "contradicted_in": list(self.contradicted_in),
        }


# A constraint seen in only one tool-set shape is indistinguishable from "that
# is just how this one sequence goes". Requiring two distinct shapes is what
# separates a transferable rule from a memorised sequence — and it is the whole
# difference between a skill that helps one task family and one that helps
# every family making the same mistake.
MIN_CONSTRAINT_CONTEXTS = 2


def extract_ordering_constraints(
    episodes: Sequence[Dict[str, Any]],
    *,
    min_support: int = MIN_SUPPORT,
    min_contexts: int = MIN_CONSTRAINT_CONTEXTS,
) -> List[OrderingConstraint]:
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
        shape = _signature(sorted(set(tools)))
        task_type = str(episode.get("task_type") or "unknown")
        for i, earlier in enumerate(tools):
            for later in tools[i + 1 :]:
                if earlier == later:
                    continue
                forward[(earlier, later)].add(shape)
                by_task_type[(earlier, later)].add(task_type)

    constraints: List[OrderingConstraint] = []
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
            OrderingConstraint(
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
    constraints: Sequence[OrderingConstraint],
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
