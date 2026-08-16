"""The orchestration asset: what one ReAct assembly is made of.

Orchestration evolution used to target :class:`~core.evolution.policies.WorkflowPolicy`,
a set of retry/budget knobs read by ``autonomous_loop``. That was the wrong
target, for a reason that has nothing to do with how the knobs were written: the
product's main axis is a **ReAct loop**, and a ReAct loop has no DAG to tune. It
re-decides every turn from whatever was assembled in front of it. So the
orchestration of a ReAct system *is* the assembly, and it has six parts:

1. **prompt fragments** — what standing instructions this task type carries;
2. **tool allowlist** — which tools are even visible (a narrower set is a better
   set: every irrelevant tool is a wrong turn the model can take);
3. **skill set** — which capabilities are offered, i.e. "the combination of
   skills", which is what a recurring skill co-occurrence should promote *into*;
4. **subagent routes** — what gets handed to a separate context entirely;
5. **retrieval and budget** — memory budget, max turns, reviewer checkpoints;
6. **intervention rules** — what to do when the loop stops making progress,
   which applies to a ReAct loop exactly as it applied to the autonomous one.

A profile is **declarative only**. It selects, orders, budgets and routes; it
never carries executable content. ``OP_WHITELIST["agent_profile"]`` has no
operation resembling ``edit_source``, and the tool allowlist can only ever
narrow: :func:`validate_profile` refuses a profile requesting a tool outside the
base grant, so an evolved profile cannot become a privilege-escalation path.

Scope resolution is explicit and ordered — user, then tenant, then deployment
default — and a profile belonging to *another* tenant is never a fallback.
Inheriting someone else's orchestration is invisible when it happens and shows
up as "the system got slower last week", which is the hardest class of problem
to attribute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── Intervention vocabulary (signals a ReAct loop can actually observe) ───────

SIGNAL_NO_PROGRESS = "no_progress"
SIGNAL_REPEATED_ACTIONS = "repeated_actions"
SIGNAL_NO_DIFF = "no_diff"
SIGNAL_REVIEWER_FLAT = "reviewer_score_flat"
SIGNAL_BUDGET_LOW = "budget_low"
SIGNAL_TOOL_ERROR_STREAK = "tool_error_streak"
ALLOWED_SIGNALS = (
    SIGNAL_NO_PROGRESS,
    SIGNAL_REPEATED_ACTIONS,
    SIGNAL_NO_DIFF,
    SIGNAL_REVIEWER_FLAT,
    SIGNAL_BUDGET_LOW,
    SIGNAL_TOOL_ERROR_STREAK,
)

# What a single ReAct turn can actually observe about itself. The rest need a
# file tree and a reviewer verdict — things a long-running loop has and one
# answer does not — so a rule keyed on them is inert on the main axis. Stated
# here rather than discovered later: a rule that can never fire looks identical,
# in the console, to one that simply has not fired yet.
REACT_OBSERVABLE_SIGNALS = frozenset(
    {SIGNAL_NO_PROGRESS, SIGNAL_REPEATED_ACTIONS, SIGNAL_TOOL_ERROR_STREAK}
)
LOOP_ONLY_SIGNALS = frozenset(
    {SIGNAL_NO_DIFF, SIGNAL_REVIEWER_FLAT, SIGNAL_BUDGET_LOW}
)

ACTION_RETRY = "retry"
ACTION_CHANGE_STRATEGY = "change_strategy"
ACTION_ROLLBACK_AND_FORK = "rollback_and_fork"
ACTION_NARROW_SCOPE = "narrow_scope"
ACTION_DELEGATE = "delegate"
ACTION_ESCALATE = "escalate"
ACTION_STOP = "stop"
ALLOWED_ACTIONS = (
    ACTION_RETRY,
    ACTION_CHANGE_STRATEGY,
    ACTION_ROLLBACK_AND_FORK,
    ACTION_NARROW_SCOPE,
    ACTION_DELEGATE,
    ACTION_ESCALATE,
    ACTION_STOP,
)

# Budget multipliers outside this band stop being tuning and start being a
# different product: a 10× budget is a cost incident, a 0.05× budget is an
# outage. The band is validated, not clamped — silently correcting an
# out-of-range value hides the fact that something proposed it.
MIN_BUDGET_MULTIPLIER = 0.25
MAX_BUDGET_MULTIPLIER = 3.0
# 0 is the default and means "no turn cap" — the main agent is unbounded unless
# a published profile deliberately fences it. Any other value must land in the
# band below; the band starts at 3 because a 1-2 turn main agent cannot finish a
# single tool round-trip.
MIN_REACT_TURNS = 3
MAX_REACT_TURNS = 80
UNBOUNDED_REACT_TURNS = 0
MIN_MEMORY_BUDGET_MS = 100
MAX_MEMORY_BUDGET_MS = 5000
MIN_LOOP_ATTEMPTS = 2
MAX_LOOP_ATTEMPTS = 20

TARGET_KIND = "agent_profile"


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
    (see :mod:`core.evolution.orchestration_gen`), because a route to an agent
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
    max_react_turns: int = UNBOUNDED_REACT_TURNS
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
            max_react_turns=int(raw.get("max_react_turns") or UNBOUNDED_REACT_TURNS),
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
        max_react_turns=UNBOUNDED_REACT_TURNS,
        budget_multiplier=1.0,
        loop_max_attempts_per_requirement=6,
        loop_strategy_change_after=2,
        intervention_rules=[
            InterventionRule(SIGNAL_NO_PROGRESS, 2, ACTION_CHANGE_STRATEGY),
            InterventionRule(SIGNAL_REPEATED_ACTIONS, 3, ACTION_NARROW_SCOPE),
            InterventionRule(SIGNAL_TOOL_ERROR_STREAK, 3, ACTION_CHANGE_STRATEGY),
            InterventionRule(SIGNAL_BUDGET_LOW, 1, ACTION_ESCALATE),
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
        if rule.signal not in ALLOWED_SIGNALS:
            problems.append(f"未知信号 {rule.signal}")
        if rule.action not in ALLOWED_ACTIONS:
            problems.append(f"未知动作 {rule.action}")
        if rule.threshold < 1:
            problems.append(f"{rule.signal} 阈值必须 ≥1（0 会让干预立刻触发）")

    if not (MIN_BUDGET_MULTIPLIER <= profile.budget_multiplier <= MAX_BUDGET_MULTIPLIER):
        problems.append(
            f"预算倍数 {profile.budget_multiplier} 超出 "
            f"[{MIN_BUDGET_MULTIPLIER}, {MAX_BUDGET_MULTIPLIER}]"
        )
    if profile.max_react_turns != UNBOUNDED_REACT_TURNS and not (
        MIN_REACT_TURNS <= profile.max_react_turns <= MAX_REACT_TURNS
    ):
        problems.append(
            f"最大轮数 {profile.max_react_turns} 超出 "
            f"[{MIN_REACT_TURNS}, {MAX_REACT_TURNS}]（{UNBOUNDED_REACT_TURNS} 表示不限）"
        )
    if not (MIN_MEMORY_BUDGET_MS <= profile.memory_budget_ms <= MAX_MEMORY_BUDGET_MS):
        problems.append(
            f"记忆检索预算 {profile.memory_budget_ms}ms 超出 "
            f"[{MIN_MEMORY_BUDGET_MS}, {MAX_MEMORY_BUDGET_MS}]"
        )
    if not (
        MIN_LOOP_ATTEMPTS
        <= profile.loop_max_attempts_per_requirement
        <= MAX_LOOP_ATTEMPTS
    ):
        problems.append(
            f"单需求最大尝试次数 {profile.loop_max_attempts_per_requirement} 超出 "
            f"[{MIN_LOOP_ATTEMPTS}, {MAX_LOOP_ATTEMPTS}]"
        )
    if profile.loop_strategy_change_after < 1:
        problems.append("换策略阈值必须 ≥1（0 会让每一轮都换策略）")

    return (not problems), problems


# ── Resolution ───────────────────────────────────────────────────────────────


def _scope_rank(scope: Dict[str, str], *, user_id: str, tenant_id: str) -> Optional[int]:
    """How specific this profile is to the subject, or ``None`` if it is not.

    Lower is more specific. Returning ``None`` for a profile owned by another
    subject is the whole point: it must not act as a fallback.
    """
    owner = str((scope or {}).get("user_id") or "")
    tenant = str((scope or {}).get("tenant_id") or "")
    if owner:
        return 0 if owner == user_id else None
    if tenant:
        return 1 if tenant == (tenant_id or "default") else None
    return 2


def pick_profile(
    profiles: Sequence[AgentProfile],
    *,
    task_type: str,
    user_id: str = "",
    tenant_id: str = "default",
) -> Optional[AgentProfile]:
    """The one profile that governs this run, or ``None``.

    A task-type-specific profile beats a general one at the same scope level:
    the narrower statement is the more deliberate one.
    """
    ranked: List[Tuple[int, int, AgentProfile]] = []
    for profile in profiles:
        if not profile.applies_to(task_type):
            continue
        rank = _scope_rank(profile.scope, user_id=user_id, tenant_id=tenant_id)
        if rank is None:
            continue
        ranked.append((rank, 0 if profile.task_types else 1, profile))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2]


def _profile_from_row(row: Any) -> AgentProfile:
    """The profile a stored row describes, with the columns believed over payload.

    ``profile_id`` / ``version`` / ``task_types`` / ``scope`` exist as real
    columns *and* inside ``payload`` (:meth:`AgentProfile.to_dict` writes them),
    so for anything published through :mod:`core.evolution.activation` the two
    agree and this is a no-op. They stop agreeing for a row written by hand or
    by a seeding script, and reading only ``payload`` then fails open in the
    worst possible direction: a missing ``task_types`` parses as ``[]``, which
    :meth:`AgentProfile.applies_to` treats as *every* task type, and a missing
    ``scope`` parses as ``{}``, which :func:`_scope_rank` treats as *every*
    subject. A profile published for one task type silently governs every run —
    reported under the id ``builtin``, because that is what
    :meth:`AgentProfile.from_dict` falls back to, so the log line naming the
    culprit names the deployment default instead.

    The two narrowing fields resolve to whichever side is non-empty rather than
    to the column unconditionally: an empty column against a populated payload
    is the same widening failure with the sides swapped. When both are populated
    and disagree the column wins — it is what the activation uniqueness check and
    the console read — and the disagreement is logged, because it means something
    wrote this row outside the activation path.
    """
    raw = dict(row.payload or {})

    # Identity: the column is the primary key and NOT NULL, so it is always the
    # more trustworthy of the two, and a wrong id here is what makes a bad
    # profile untraceable in the logs.
    raw["profile_id"] = row.profile_id
    raw["version"] = row.version

    col_types = [str(t) for t in (row.task_types or [])]
    payload_types = [str(t) for t in (raw.get("task_types") or [])]
    if col_types and payload_types and set(col_types) != set(payload_types):
        logger.warning(
            "[agent-profile] %s: task_types column %s != payload %s, using column",
            row.profile_id, col_types, payload_types,
        )
    raw["task_types"] = col_types or payload_types

    col_scope = dict(row.scope or {})
    payload_scope = dict(raw.get("scope") or {})
    if col_scope and payload_scope and col_scope != payload_scope:
        logger.warning(
            "[agent-profile] %s: scope column %s != payload %s, using column",
            row.profile_id, col_scope, payload_scope,
        )
    raw["scope"] = col_scope or payload_scope

    return AgentProfile.from_dict(raw)


def load_active_profile(
    *,
    task_type: str = "chat",
    user_id: str = "",
    tenant_id: str = "default",
) -> AgentProfile:
    """The profile in force for this run.

    Read fresh on every call. Resolving once at import — which is what the old
    orchestration policy did — means a published change needs a process restart
    to take effect and different replicas disagree in the meantime, neither of
    which is acceptable for an asset whose entire purpose is to change behaviour
    between runs.
    """
    from core.db.engine import SessionLocal
    from core.db.models.evolution import EvolutionAgentProfile

    try:
        with SessionLocal() as db:
            rows = (
                db.query(EvolutionAgentProfile)
                .filter(EvolutionAgentProfile.is_active.is_(True))
                .order_by(EvolutionAgentProfile.updated_at.desc())
                .all()
            )
            profiles = [_profile_from_row(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent-profile] load failed, using built-in: %s", exc)
        return builtin_profile()

    chosen = pick_profile(
        profiles, task_type=task_type, user_id=user_id, tenant_id=tenant_id
    )
    if chosen is None:
        return builtin_profile()
    ok, problems = validate_profile(chosen)
    if not ok:
        # A stored profile that no longer validates is not applied. The
        # alternative — applying it partially — produces a configuration nobody
        # reviewed and nobody can reproduce.
        logger.warning(
            "[agent-profile] stored profile %s rejected at load: %s",
            chosen.profile_id,
            problems,
        )
        return builtin_profile()
    return chosen
