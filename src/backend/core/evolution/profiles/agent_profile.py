"""Agent profiles: one ReAct configuration expressed as data, with validation."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from core.evolution.profiles import agent_profile_constants

@dataclass
class InterventionRule:
    """signal → threshold → action."""

    signal: str
    threshold: int
    action: str
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal": self.signal,
            "threshold": self.threshold,
            "action": self.action,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "InterventionRule":
        return cls(
            signal=str(raw.get("signal") or ""),
            threshold=int(raw.get("threshold") or 0),
            action=str(raw.get("action") or ""),
            note=str(raw.get("note") or ""),
        )


@dataclass
class SubagentRoute:
    """Hand this shape of sub-task to a separate context.

    ``agent_id`` points at a real sub-agent; the route is only the *decision to
    delegate*. Creating the sub-agent is a separate, separately-approved change
    (see :mod:`core.evolution.generators.orchestration_gen`), because a route to an agent
    that does not exist is a dead end and an agent nobody routes to is dead
    weight — they have to be reviewed as one change but recorded as two.
    """

    agent_id: str
    when: str = ""
    task_types: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"agent_id": self.agent_id, "when": self.when, "task_types": list(self.task_types)}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SubagentRoute":
        return cls(
            agent_id=str(raw.get("agent_id") or ""),
            when=str(raw.get("when") or ""),
            task_types=[str(t) for t in (raw.get("task_types") or [])],
        )


@dataclass
class AgentProfile:
    """One assembled ReAct configuration, as data."""

    profile_id: str = "builtin"
    version: str = "v0"
    # Which tasks this profile governs. Empty means "every task in scope" — the
    # deployment default. A profile with task types is only consulted for those,
    # which is how a capability learned in one family stops leaking into others.
    task_types: List[str] = field(default_factory=list)
    scope: Dict[str, str] = field(default_factory=dict)

    prompt_fragments: List[str] = field(default_factory=list)
    # ``None`` means "no restriction beyond the base grant". An empty list means
    # "no tools", which is a legitimate profile for a pure-reasoning task type
    # and must not be confused with the former.
    tool_allowlist: Optional[List[str]] = None
    # Which skills this task type may load at all. Empty means "no restriction"
    # — skill loading keeps its own logic and this only ever narrows it, and
    # only for task types an approved profile actually covers.
    skill_ids: List[str] = field(default_factory=list)
    subagent_routes: List[SubagentRoute] = field(default_factory=list)

    memory_budget_ms: int = 600
    # 0 = 不限（默认）。See UNBOUNDED_REACT_TURNS: the main agent's round ceiling
    # was removed, so a turn budget here is an opt-in narrowing a published
    # profile performs deliberately, not a floor every profile inherits.
    max_react_turns: int = agent_profile_constants.UNBOUNDED_REACT_TURNS
    budget_multiplier: float = 1.0
    reviewer_checkpoints: List[str] = field(default_factory=list)
    intervention_rules: List[InterventionRule] = field(default_factory=list)

    # The autonomous loop's own caps. Held as their own fields rather than
    # derived from ``max_react_turns``: the two loops bound different things —
    # turns inside one answer versus attempts at one requirement across many
    # answers — and a ratio between them would be an invented relationship that
    # quietly changes one whenever the other is tuned.
    loop_max_attempts_per_requirement: int = 6
    loop_strategy_change_after: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "task_types": list(self.task_types),
            "scope": dict(self.scope),
            "prompt_fragments": list(self.prompt_fragments),
            "tool_allowlist": None if self.tool_allowlist is None else list(self.tool_allowlist),
            "skill_ids": list(self.skill_ids),
            "subagent_routes": [r.to_dict() for r in self.subagent_routes],
            "memory_budget_ms": self.memory_budget_ms,
            "max_react_turns": self.max_react_turns,
            "budget_multiplier": self.budget_multiplier,
            "reviewer_checkpoints": list(self.reviewer_checkpoints),
            "intervention_rules": [r.to_dict() for r in self.intervention_rules],
            "loop_max_attempts_per_requirement": self.loop_max_attempts_per_requirement,
            "loop_strategy_change_after": self.loop_strategy_change_after,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AgentProfile":
        allowlist = raw.get("tool_allowlist", None)
        return cls(
            profile_id=str(raw.get("profile_id") or "builtin"),
            version=str(raw.get("version") or "v0"),
            task_types=[str(t) for t in (raw.get("task_types") or [])],
            scope=dict(raw.get("scope") or {}),
            prompt_fragments=[str(f) for f in (raw.get("prompt_fragments") or [])],
            tool_allowlist=None if allowlist is None else [str(t) for t in allowlist],
            skill_ids=[str(s) for s in (raw.get("skill_ids") or [])],
            subagent_routes=[
                SubagentRoute.from_dict(r) for r in (raw.get("subagent_routes") or [])
            ],
            memory_budget_ms=int(raw.get("memory_budget_ms") or 600),
            # Missing key → unbounded, not an invented ceiling: a profile
            # published for unrelated reasons (tool allowlist, memory budget)
            # must not silently re-fence the main loop.
            max_react_turns=int(raw.get("max_react_turns") or agent_profile_constants.UNBOUNDED_REACT_TURNS),
            budget_multiplier=float(raw.get("budget_multiplier") or 1.0),
            reviewer_checkpoints=[str(c) for c in (raw.get("reviewer_checkpoints") or [])],
            intervention_rules=[
                InterventionRule.from_dict(r) for r in (raw.get("intervention_rules") or [])
            ],
            loop_max_attempts_per_requirement=int(
                raw.get("loop_max_attempts_per_requirement") or 6
            ),
            loop_strategy_change_after=int(raw.get("loop_strategy_change_after") or 2),
        )

    def applies_to(self, task_type: str) -> bool:
        return not self.task_types or str(task_type or "chat") in self.task_types


def builtin_profile() -> AgentProfile:
    """The deployment default.

    Reproduces today's behaviour exactly — no tool restriction, no forced skill
    set, the retrieval budget and turn cap the runtime already uses — so
    installing profile resolution changes nothing until a profile is published.
    """
    return AgentProfile(
        profile_id="builtin",
        version="v0",
        tool_allowlist=None,
        memory_budget_ms=600,
        # The main agent's runtime default, reproduced exactly — which is now
        # "no cap". This field only bounds the loop when a published profile
        # sets it on purpose.
        max_react_turns=agent_profile_constants.UNBOUNDED_REACT_TURNS,
        budget_multiplier=1.0,
        loop_max_attempts_per_requirement=6,
        loop_strategy_change_after=2,
        intervention_rules=[
            InterventionRule(agent_profile_constants.SIGNAL_NO_PROGRESS, 2, agent_profile_constants.ACTION_CHANGE_STRATEGY),
            InterventionRule(agent_profile_constants.SIGNAL_REPEATED_ACTIONS, 3, agent_profile_constants.ACTION_NARROW_SCOPE),
            InterventionRule(agent_profile_constants.SIGNAL_TOOL_ERROR_STREAK, 3, agent_profile_constants.ACTION_CHANGE_STRATEGY),
            InterventionRule(agent_profile_constants.SIGNAL_BUDGET_LOW, 1, agent_profile_constants.ACTION_ESCALATE),
        ],
    )


def validate_profile(
    profile: AgentProfile,
    *,
    base_tools: Optional[Sequence[str]] = None,
    known_skill_ids: Optional[Sequence[str]] = None,
    known_agent_ids: Optional[Sequence[str]] = None,
) -> Tuple[bool, List[str]]:
    """Structural and security validation.

    ``base_tools`` is the authority this profile is allowed to draw from. A
    profile naming a tool outside it is refused — orchestration evolution may
    narrow authority and may never widen it, and this is the check that makes
    "narrow only" a property of the system rather than an intention.
    """
    problems: List[str] = []

    if not profile.profile_id:
        problems.append("profile_id 不能为空")

    if profile.tool_allowlist is not None and base_tools is not None:
        widened = sorted(set(profile.tool_allowlist) - set(base_tools))
        if widened:
            problems.append(f"工具授权越界（只允许收敛，不允许扩权）: {widened}")

    if known_skill_ids is not None:
        unknown = sorted(set(profile.skill_ids) - set(known_skill_ids))
        if unknown:
            problems.append(f"引用了不存在的技能: {unknown}")

    seen_agents = set()
    for route in profile.subagent_routes:
        if not route.agent_id:
            problems.append("子智能体路由缺少 agent_id")
            continue
        if route.agent_id in seen_agents:
            problems.append(f"重复的子智能体路由: {route.agent_id}")
        seen_agents.add(route.agent_id)
        if known_agent_ids is not None and route.agent_id not in set(known_agent_ids):
            problems.append(f"路由指向不存在的子智能体: {route.agent_id}")

    for rule in profile.intervention_rules:
        if rule.signal not in agent_profile_constants.ALLOWED_SIGNALS:
            problems.append(f"未知信号 {rule.signal}")
        if rule.action not in agent_profile_constants.ALLOWED_ACTIONS:
            problems.append(f"未知动作 {rule.action}")
        if rule.threshold < 1:
            problems.append(f"{rule.signal} 阈值必须 ≥1（0 会让干预立刻触发）")

    if not (agent_profile_constants.MIN_BUDGET_MULTIPLIER <= profile.budget_multiplier <= agent_profile_constants.MAX_BUDGET_MULTIPLIER):
        problems.append(
            f"预算倍数 {profile.budget_multiplier} 超出 "
            f"[{agent_profile_constants.MIN_BUDGET_MULTIPLIER}, {agent_profile_constants.MAX_BUDGET_MULTIPLIER}]"
        )
    if profile.max_react_turns != agent_profile_constants.UNBOUNDED_REACT_TURNS and not (
        agent_profile_constants.MIN_REACT_TURNS <= profile.max_react_turns <= agent_profile_constants.MAX_REACT_TURNS
    ):
        problems.append(
            f"最大轮数 {profile.max_react_turns} 超出 "
            f"[{agent_profile_constants.MIN_REACT_TURNS}, {agent_profile_constants.MAX_REACT_TURNS}]（{agent_profile_constants.UNBOUNDED_REACT_TURNS} 表示不限）"
        )
    if not (agent_profile_constants.MIN_MEMORY_BUDGET_MS <= profile.memory_budget_ms <= agent_profile_constants.MAX_MEMORY_BUDGET_MS):
        problems.append(
            f"记忆检索预算 {profile.memory_budget_ms}ms 超出 "
            f"[{agent_profile_constants.MIN_MEMORY_BUDGET_MS}, {agent_profile_constants.MAX_MEMORY_BUDGET_MS}]"
        )
    if not (
        agent_profile_constants.MIN_LOOP_ATTEMPTS
        <= profile.loop_max_attempts_per_requirement
        <= agent_profile_constants.MAX_LOOP_ATTEMPTS
    ):
        problems.append(
            f"单需求最大尝试次数 {profile.loop_max_attempts_per_requirement} 超出 "
            f"[{agent_profile_constants.MIN_LOOP_ATTEMPTS}, {agent_profile_constants.MAX_LOOP_ATTEMPTS}]"
        )
    if profile.loop_strategy_change_after < 1:
        problems.append("换策略阈值必须 ≥1（0 会让每一轮都换策略）")

    return (not problems), problems
