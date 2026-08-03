"""Producing orchestration candidates: profiles, and the sub-agents they route to.

Orchestration is the last layer to evolve and the one with the widest blast
radius, so its generators are the most conservative in the system. Three shapes
are produced, in increasing order of how much they change:

1. **Tool convergence.** A task type that never uses most of the tools it is
   offered gets a narrower allowlist. This is the cheapest real improvement
   available: every irrelevant tool in view is a wrong turn the model can take,
   and removing one costs nothing if it was genuinely never used.
2. **Skill combination.** Skills that keep being opened together on the same
   task type become that task type's declared skill set — the answer to "what
   does a recurring skill co-occurrence promote *into*", which was previously
   "a workflow DAG the ReAct loop never executes".
3. **Sub-agent derivation.** A long, tool-divergent, context-hungry chunk of
   work becomes a separate agent with its own context and a *narrower* grant.

The third is the one you have to be careful with, and the care is structural
rather than advisory:

* the derived agent's tools must be a **subset** of the parent's — enforced in
  :func:`~core.evolution.ir.validate_ir` through the same escalation invariant
  that governs every other candidate;
* the derived agent **may not delegate** — one layer, or a single bad routing
  decision compounds at every level along with its budget;
* the route that sends work to it is a **prompt change** to the parent, and goes
  through prompt review rather than riding along invisibly with the agent's
  creation.

Nothing here activates anything. Every function returns a proposal.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────

# Occurrences of a task type before its orchestration is worth fixing at all.
MIN_TASK_TYPE_SUPPORT = 8

# "Never used" is *absence of evidence*, and the two failure directions here are
# not symmetric. Wrongly keeping a tool costs one extra line in a tool list.
# Wrongly removing one silently deletes a capability — a user asks a question
# that needs the database and the agent simply cannot, with no error and nothing
# in the console explaining why.
#
# So the bar is set where "never used" starts to mean something. At 10 episodes
# it meant nothing: a dev corpus of 17 chats produced a proposal to remove
# database access, report export, site publishing and scheduled tasks, none of
# which anyone had happened to exercise that week.
MIN_EPISODES_FOR_TOOL_CONVERGENCE = 200
# Never dropping below this many tools: a profile that narrows to almost nothing
# has stopped being a profile and become a different product for that task type.
MIN_TOOLS_AFTER_CONVERGENCE = 3
# At most this share of the offered tools may go in one proposal. A convergence
# that removes most of the toolbox is not tuning; it is a different product, and
# it should be arrived at over several reviewed steps rather than one click.
MAX_CONVERGENCE_FRACTION = 0.34

# Task types that are the *absence* of a classification rather than a kind of
# work. Narrowing these narrows everything: "chat" is where every unclassified
# request lands, so it has no stable tool profile by construction, and the tools
# it "never uses" are simply the ones that week's traffic did not reach for.
CATCH_ALL_TASK_TYPES = frozenset({"chat", "", "unknown", "default"})

# Skills opened together in this fraction of a task type's episodes belong to it.
CO_OCCURRENCE_RATIO = 0.6
MIN_CO_OCCURRENCE = 4

# Sub-agent derivation. All four must hold — see the module docstring.
SUBAGENT_MIN_STEPS = 6
SUBAGENT_MIN_OCCURRENCES = 4
# The chunk's tools must be substantially *different* from what the rest of the
# work uses; overlapping heavily means splitting the context buys nothing and
# costs a hand-off.
SUBAGENT_MAX_TOOL_OVERLAP = 0.4
# Stalling this often is the signal that one context cannot hold the task.
SUBAGENT_MIN_STALL_RATE = 0.25


@dataclass
class OrchestrationProposal:
    """A proposed change to how a task type is assembled."""

    kind: str
    task_type: str
    rationale: str
    payload: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    support: int = 0

    @property
    def asset_id(self) -> str:
        seed = f"{self.kind}|{self.task_type}|{sorted(self.payload.items())!r}"
        return "auto-orc-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "task_type": self.task_type,
            "asset_id": self.asset_id,
            "rationale": self.rationale,
            "payload": dict(self.payload),
            "support": self.support,
            "evidence_refs": self.evidence_refs[:20],
        }


@dataclass
class SubagentProposal:
    """A proposed sub-agent, plus the route that would send work to it."""

    name: str
    responsibility: str
    required_skills: List[str]
    tools_allowlist: List[str]
    task_types: List[str]
    route_hint: str
    rationale: str
    evidence_refs: List[str] = field(default_factory=list)
    support: int = 0

    @property
    def agent_id(self) -> str:
        seed = f"subagent|{self.name}|{sorted(self.tools_allowlist)!r}"
        return "auto-agent-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "responsibility": self.responsibility,
            "required_skills": list(self.required_skills),
            "tools_allowlist": list(self.tools_allowlist),
            "task_types": list(self.task_types),
            "route_hint": self.route_hint,
            "rationale": self.rationale,
            "support": self.support,
            "evidence_refs": self.evidence_refs[:20],
            # Stated explicitly so the invariant has something to check rather
            # than an absence to interpret.
            "may_delegate": False,
        }


# ── 1. Tool convergence ──────────────────────────────────────────────────────


def converge_tools(
    views: Sequence[Dict[str, Any]],
    *,
    offered_tools: Dict[str, List[str]],
) -> List[OrchestrationProposal]:
    """Narrow each task type's tool allowlist to what it demonstrably uses.

    ``offered_tools`` maps task type → the tools that *were* available. Without
    it there is no way to tell "this tool was never useful here" from "this tool
    was never offered here", and only the first is a reason to narrow anything.
    """
    used: Dict[str, Set[str]] = {}
    counts: Dict[str, int] = {}
    evidence: Dict[str, List[str]] = {}
    for view in views:
        task_type = str(view.get("task_type") or "chat")
        counts[task_type] = counts.get(task_type, 0) + 1
        bucket = used.setdefault(task_type, set())
        for tool in view.get("tool_sequence") or []:
            bucket.add(str(tool))
        evidence.setdefault(task_type, []).append(str(view.get("episode_id") or ""))

    proposals: List[OrchestrationProposal] = []
    for task_type, episodes in counts.items():
        if task_type in CATCH_ALL_TASK_TYPES:
            # Narrowing the catch-all is narrowing the product.
            continue
        if episodes < MIN_EPISODES_FOR_TOOL_CONVERGENCE:
            continue
        offered = [str(t) for t in (offered_tools.get(task_type) or [])]
        if not offered:
            continue
        actually_used = used.get(task_type, set())
        keep = [tool for tool in offered if tool in actually_used]
        dropped = [tool for tool in offered if tool not in actually_used]
        if not dropped or len(keep) < MIN_TOOLS_AFTER_CONVERGENCE:
            continue
        if len(dropped) > max(1, int(len(offered) * MAX_CONVERGENCE_FRACTION)):
            # Too much at once to be a tuning step. Reported so an operator can
            # see the finding, but not proposed as a change.
            logger.info(
                "[orchestration-gen] %s: %d/%d tools unused — too large a cut to "
                "propose in one step, skipping",
                task_type, len(dropped), len(offered),
            )
            continue
        proposals.append(
            OrchestrationProposal(
                kind="tool_convergence",
                task_type=task_type,
                support=episodes,
                rationale=(
                    f"{task_type} 类任务的 {episodes} 条执行中，"
                    f"{len(dropped)} 个工具一次都没被用过——"
                    "每个无关工具都是一次可能走错的分支"
                ),
                payload={"tool_allowlist": keep, "dropped": dropped},
                evidence_refs=evidence.get(task_type, [])[:20],
            )
        )
    return proposals


# ── 2. Skill combination ─────────────────────────────────────────────────────


def converge_skills(views: Sequence[Dict[str, Any]]) -> List[OrchestrationProposal]:
    """Skills consistently opened together on one task type become its skill set.

    Reads ``skills_opened`` rather than what was offered. What the model was
    handed says nothing about which capabilities the task needs; what it chose
    to open does.
    """
    by_type_total: Dict[str, int] = {}
    by_type_skill: Dict[str, Dict[str, int]] = {}
    evidence: Dict[str, List[str]] = {}

    for view in views:
        task_type = str(view.get("task_type") or "chat")
        by_type_total[task_type] = by_type_total.get(task_type, 0) + 1
        bucket = by_type_skill.setdefault(task_type, {})
        for opened in view.get("skills_opened") or []:
            skill_id = str(opened.get("skill_id") or "")
            if skill_id:
                bucket[skill_id] = bucket.get(skill_id, 0) + 1
        evidence.setdefault(task_type, []).append(str(view.get("episode_id") or ""))

    proposals: List[OrchestrationProposal] = []
    for task_type, total in by_type_total.items():
        if total < MIN_TASK_TYPE_SUPPORT:
            continue
        frequent = sorted(
            (
                skill_id
                for skill_id, hits in by_type_skill.get(task_type, {}).items()
                if hits >= MIN_CO_OCCURRENCE and hits / total >= CO_OCCURRENCE_RATIO
            )
        )
        if len(frequent) < 2:
            continue
        proposals.append(
            OrchestrationProposal(
                kind="skill_combination",
                task_type=task_type,
                support=total,
                rationale=(
                    f"{task_type} 类任务中，{len(frequent)} 个技能在 "
                    f"{CO_OCCURRENCE_RATIO:.0%} 以上的执行里被同时打开——"
                    "这组能力应当固化为该类任务的默认组合，而不是每次重新挑"
                ),
                payload={"skill_ids": frequent},
                evidence_refs=evidence.get(task_type, [])[:20],
            )
        )
    return proposals


# ── 3. Sub-agent derivation ──────────────────────────────────────────────────


def _tool_overlap(a: Sequence[str], b: Sequence[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def derive_subagents(
    views: Sequence[Dict[str, Any]],
    *,
    skill_segments: Sequence[Dict[str, Any]],
    parent_tools: Sequence[str],
) -> List[SubagentProposal]:
    """Chunks of work that belong in their own context.

    ``skill_segments`` describes recurring units of work — typically a skill and
    the tool run that follows it being opened:

        {"skill_id", "name", "steps", "tools", "occurrences",
         "task_types", "stalled", "episode_ids"}

    All four conditions must hold. Each excludes a different way of being wrong:
    a short chunk gains nothing from a hand-off; a chunk sharing the parent's
    tools cannot be given a narrower grant, so splitting it buys no safety; a
    chunk whose output the parent needs in full has to stay in the parent's
    context anyway; and without stalling there is no evidence the single context
    was ever the constraint.
    """
    parent = {str(t) for t in parent_tools}
    task_type_totals: Dict[str, int] = {}
    for view in views:
        task_type = str(view.get("task_type") or "chat")
        task_type_totals[task_type] = task_type_totals.get(task_type, 0) + 1

    proposals: List[SubagentProposal] = []
    for segment in skill_segments:
        occurrences = int(segment.get("occurrences") or 0)
        steps = int(segment.get("steps") or 0)
        tools = [str(t) for t in (segment.get("tools") or [])]
        stalled = int(segment.get("stalled") or 0)

        if occurrences < SUBAGENT_MIN_OCCURRENCES:
            continue
        if steps < SUBAGENT_MIN_STEPS:
            continue
        if not tools:
            continue

        outside = sorted(set(tools) - parent)
        if outside:
            # Refused before it can become a candidate. The IR gate would also
            # catch this, but a proposal that exists only to be rejected wastes
            # a reviewer's attention and looks, in the console, like the system
            # repeatedly trying to escalate its own privileges.
            logger.info(
                "[orchestration-gen] segment %s skipped: tools outside parent grant %s",
                segment.get("skill_id"),
                outside,
            )
            continue

        overlap = _tool_overlap(tools, parent)
        if overlap > SUBAGENT_MAX_TOOL_OVERLAP:
            continue

        stall_rate = stalled / occurrences if occurrences else 0.0
        if stall_rate < SUBAGENT_MIN_STALL_RATE:
            continue

        task_types = [str(t) for t in (segment.get("task_types") or [])]
        name = str(segment.get("name") or segment.get("skill_id") or "").strip()
        if not name:
            continue

        proposals.append(
            SubagentProposal(
                name=name,
                responsibility=str(segment.get("responsibility") or "")[:400]
                or f"独立承担 {name} 这段工作，只返回结论",
                required_skills=[str(segment.get("skill_id"))]
                if segment.get("skill_id")
                else [],
                tools_allowlist=sorted(set(tools)),
                task_types=task_types,
                route_hint=(
                    f"当任务属于 {'、'.join(task_types) or '该类场景'} 且需要"
                    f"{name} 这段工作时，派给子智能体处理，只取回结论"
                ),
                rationale=(
                    f"该段工作重复出现 {occurrences} 次、平均 {steps} 步，"
                    f"工具集与主体重合度仅 {overlap:.0%}，且 {stall_rate:.0%} 的执行中"
                    "主体在此处停滞——独立上下文可以同时收窄授权并腾出主上下文"
                ),
                evidence_refs=[str(e) for e in (segment.get("episode_ids") or [])][:20],
                support=occurrences,
            )
        )
    return proposals


def segments_from_views(views: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Recurring units of work, derived from what followed each skill open.

    A "segment" is the run of tool calls after the model opened a skill. That is
    the closest thing the evidence has to a self-contained chunk of work: the
    model itself decided that stretch belonged to one capability.
    """
    segments: Dict[str, Dict[str, Any]] = {}
    for view in views:
        task_type = str(view.get("task_type") or "chat")
        stalled = bool(view.get("stalled"))
        for opened in view.get("skills_opened") or []:
            skill_id = str(opened.get("skill_id") or "")
            if not skill_id:
                continue
            after = [str(t) for t in (opened.get("tools_after_open") or [])]
            entry = segments.setdefault(
                skill_id,
                {
                    "skill_id": skill_id,
                    "name": str(opened.get("display_name") or skill_id),
                    "responsibility": str(opened.get("description") or ""),
                    "tools": [],
                    "steps": 0,
                    "occurrences": 0,
                    "task_types": [],
                    "stalled": 0,
                    "episode_ids": [],
                },
            )
            entry["occurrences"] += 1
            entry["steps"] = max(entry["steps"], len(after))
            for tool in after:
                if tool not in entry["tools"]:
                    entry["tools"].append(tool)
            if task_type not in entry["task_types"]:
                entry["task_types"].append(task_type)
            if stalled:
                entry["stalled"] += 1
            episode_id = str(view.get("episode_id") or "")
            if episode_id:
                entry["episode_ids"].append(episode_id)
    return list(segments.values())


# ── Assembling a profile from the proposals ──────────────────────────────────


def profile_from_proposals(
    proposals: Sequence[OrchestrationProposal],
    *,
    task_type: str,
    base,
    scope: Optional[Dict[str, str]] = None,
):
    """Fold this task type's proposals into one candidate profile.

    One profile per task type rather than one per proposal: they describe the
    same assembly, and reviewing "narrow the tools" separately from "and also
    always load these two skills" would hide the fact that the second is only
    safe given the first.
    """
    from core.evolution.agent_profile import AgentProfile

    merged = AgentProfile.from_dict(base.to_dict())
    merged.profile_id = f"auto-{task_type}"
    merged.task_types = [task_type]
    merged.scope = dict(scope or {})
    version_seed: List[str] = []

    for proposal in proposals:
        if proposal.task_type != task_type:
            continue
        if proposal.kind == "tool_convergence":
            merged.tool_allowlist = list(proposal.payload.get("tool_allowlist") or [])
        elif proposal.kind == "skill_combination":
            merged.skill_ids = list(proposal.payload.get("skill_ids") or [])
        version_seed.append(proposal.asset_id)

    merged.version = "v" + hashlib.sha256(
        "|".join(sorted(version_seed)).encode("utf-8")
    ).hexdigest()[:8]
    return merged


def generate_orchestration_proposals(
    views: Sequence[Dict[str, Any]],
    *,
    offered_tools: Dict[str, List[str]],
    parent_tools: Sequence[str],
) -> Tuple[List[OrchestrationProposal], List[SubagentProposal]]:
    """Every orchestration proposal this corpus supports."""
    proposals: List[OrchestrationProposal] = []
    proposals.extend(converge_tools(views, offered_tools=offered_tools))
    proposals.extend(converge_skills(views))
    subagents = derive_subagents(
        views,
        skill_segments=segments_from_views(views),
        parent_tools=parent_tools,
    )
    return proposals, subagents
