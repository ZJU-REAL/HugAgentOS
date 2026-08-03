"""GCE tickets 20 / 21 — the orchestration asset and memory operations.

The orchestration asset is now :class:`~core.evolution.agent_profile.AgentProfile`
and the autonomous loop reads a *projection* of it. These tests hold the same
invariants as before — the built-in must reproduce today's constants, the action
allow-list must refuse anything resembling code execution, the budget must be
bounded — against the asset that actually governs behaviour.
"""

import pytest

from core.evolution import agent_profile as AP
from core.evolution import memory_ops as MO
from core.evolution import policies as PL


# ── The orchestration asset ──────────────────────────────────────────────────


def test_builtin_profile_reproduces_todays_constants():
    """Adopting profile resolution must be a no-op until someone publishes."""
    policy = PL.policy_from_profile(AP.builtin_profile())
    assert policy.max_attempts_per_requirement == 6
    assert policy.strategy_change_after == 2
    # The main ReAct agent's existing cap, unchanged.
    assert AP.builtin_profile().max_react_turns == 50


def test_longer_stall_escalates_rather_than_repeating_the_mild_response():
    profile = AP.builtin_profile()
    profile.intervention_rules.append(
        AP.InterventionRule(AP.SIGNAL_NO_PROGRESS, 6, AP.ACTION_STOP)
    )
    policy = PL.policy_from_profile(profile)
    assert policy.action_for({PL.SIGNAL_NO_PROGRESS: 2}) == PL.ACTION_CHANGE_STRATEGY
    # Six stalled iterations must not keep re-issuing the response that already
    # failed to help.
    assert policy.action_for({PL.SIGNAL_NO_PROGRESS: 6}) == PL.ACTION_STOP


def test_no_signal_yields_no_intervention():
    assert PL.policy_from_profile(AP.builtin_profile()).action_for({}) is None


def test_profile_rejects_actions_outside_the_allow_list():
    profile = AP.builtin_profile()
    profile.intervention_rules = [
        AP.InterventionRule("no_progress", 2, "exec_arbitrary_code")
    ]
    ok, problems = AP.validate_profile(profile)
    # Arbitrary self-modifying behaviour is the one capability refused outright.
    assert ok is False and any("未知动作" in p for p in problems)


def test_profile_rejects_an_unbounded_budget_multiplier():
    profile = AP.builtin_profile()
    profile.budget_multiplier = 50.0
    ok, problems = AP.validate_profile(profile)
    # This is how a policy change becomes a runaway spend unnoticed.
    assert ok is False and any("预算倍数" in p for p in problems)


def test_profile_rejects_non_positive_thresholds():
    profile = AP.builtin_profile()
    profile.intervention_rules = [
        AP.InterventionRule("no_progress", 0, AP.ACTION_RETRY)
    ]
    ok, _ = AP.validate_profile(profile)
    assert ok is False


def test_a_profile_may_never_widen_tool_authority():
    """The invariant that stops orchestration evolution being an escalation path."""
    profile = AP.builtin_profile()
    profile.tool_allowlist = ["search_web", "shell_exec"]
    ok, problems = AP.validate_profile(profile, base_tools=["search_web", "fetch_page"])
    assert ok is False and any("工具授权越界" in p for p in problems)

    # Narrowing is the whole point, and must pass.
    profile.tool_allowlist = ["search_web"]
    assert AP.validate_profile(profile, base_tools=["search_web", "fetch_page"])[0]


def test_valid_profile_passes():
    ok, problems = AP.validate_profile(AP.builtin_profile())
    assert ok is True and problems == []


def test_profile_roundtrips_through_dict():
    original = AP.builtin_profile()
    restored = AP.AgentProfile.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


def test_profile_load_degrades_to_builtin_when_store_is_unavailable():
    # No database in this test context → must degrade, not raise.
    assert AP.load_active_profile(task_type="nonexistent").profile_id == "builtin"


# ── Scope resolution: the bug the tenant parameter was meant to fix ──────────


def _profile(profile_id, *, scope=None, task_types=()):
    profile = AP.builtin_profile()
    profile.profile_id = profile_id
    profile.scope = dict(scope or {})
    profile.task_types = list(task_types)
    return profile


def test_a_tenants_profile_wins_over_the_deployment_default():
    chosen = AP.pick_profile(
        [_profile("global"), _profile("acme", scope={"tenant_id": "acme"})],
        task_type="chat",
        tenant_id="acme",
    )
    assert chosen.profile_id == "acme"


def test_one_tenants_profile_never_serves_another():
    """Inheriting someone else's orchestration is invisible when it happens."""
    chosen = AP.pick_profile(
        [_profile("acme", scope={"tenant_id": "acme"})], task_type="chat", tenant_id="other"
    )
    assert chosen is None


def test_a_users_own_profile_outranks_their_tenants():
    chosen = AP.pick_profile(
        [
            _profile("tenant", scope={"tenant_id": "acme"}),
            _profile("mine", scope={"user_id": "u1"}),
        ],
        task_type="chat",
        user_id="u1",
        tenant_id="acme",
    )
    assert chosen.profile_id == "mine"


def test_a_task_type_specific_profile_outranks_a_general_one():
    chosen = AP.pick_profile(
        [_profile("general"), _profile("finance", task_types=["finance"])],
        task_type="finance",
    )
    assert chosen.profile_id == "finance"


def test_a_profile_for_another_task_type_is_not_used():
    assert (
        AP.pick_profile([_profile("finance", task_types=["finance"])], task_type="chat")
        is None
    )


# ── Memory operations (ticket 21) ────────────────────────────────────────────


def test_preference_conflict_is_diagnosed_as_a_policy_gap_not_stale_content():
    """The deck-vs-spreadsheet case.

    Deleting a still-valid long-term preference to fix a one-turn override
    trades a small problem for a larger one.
    """
    result = MO.diagnose_preference_conflict(
        injected_preference="优先用演示稿汇报",
        current_instruction="只给我 Excel，不要演示稿",
        outcome_failed=True,
        prompt_declares_priority=False,
    )
    assert result["verdict"] == "retrieval_policy"
    assert result["policy_changes"]
    assert result["policy_changes"][0]["rule"] == MO.SUPPRESSED_BY_CURRENT_REQUEST


def test_conflict_with_priority_already_declared_points_at_the_content():
    result = MO.diagnose_preference_conflict(
        injected_preference="优先用演示稿",
        current_instruction="只要 Excel",
        outcome_failed=True,
        prompt_declares_priority=True,
    )
    assert result["verdict"] == "memory_content"


def test_the_preference_is_down_weighted_never_deleted():
    result = MO.diagnose_preference_conflict(
        injected_preference="优先用演示稿",
        current_instruction="只要 Excel",
        outcome_failed=True,
        prompt_declares_priority=False,
    )
    operations = {op["operation"] for op in result["ops"]}
    assert MO.OP_REWEIGHT in operations
    # A later turn must still be able to surface the preference.
    assert MO.OP_DELETE not in operations


def test_no_conflict_produces_no_operations():
    result = MO.diagnose_preference_conflict(
        injected_preference="",
        current_instruction="随便",
        outcome_failed=False,
        prompt_declares_priority=False,
    )
    assert result["verdict"] == "no_conflict" and result["ops"] == []


def test_replay_plan_is_two_sided():
    """A one-sided replay would approve a change that simply forgot the preference."""
    plan = MO.build_replay_plan(
        [MO.RetrievalPolicyChange(rule="r", description="d")]
    )
    names = {c["name"] for c in plan["cohorts"]}
    assert names == {"honours_standing_preference", "respects_explicit_override"}
    assert all(c["must_pass"] for c in plan["cohorts"])


# ── Automation ceiling ───────────────────────────────────────────────────────


def test_reweight_may_apply_automatically():
    op = MO.MemoryOp(operation=MO.OP_REWEIGHT, reason="conflict")
    assert op.requires_human is False


def test_deletion_always_requires_a_human():
    assert MO.MemoryOp(operation=MO.OP_DELETE).requires_human is True


def test_sensitive_or_cross_user_changes_require_a_human_even_when_reweighting():
    # The asymmetry is deliberate: a wrong reweight is recoverable, a
    # cross-user leak is not.
    assert MO.MemoryOp(operation=MO.OP_REWEIGHT, sensitive=True).requires_human is True
    assert MO.MemoryOp(operation=MO.OP_REWEIGHT, cross_user=True).requires_human is True


def test_operations_partition_by_approval_requirement():
    auto, manual = MO.partition_by_approval(
        [
            MO.MemoryOp(operation=MO.OP_REWEIGHT),
            MO.MemoryOp(operation=MO.OP_DELETE),
            MO.MemoryOp(operation=MO.OP_REWEIGHT, sensitive=True),
        ]
    )
    assert len(auto) == 1 and len(manual) == 2


def test_scope_cannot_widen_beyond_the_allowed_set():
    ok, reason = MO.validate_scope("global")
    assert ok is False and "global" in reason
    assert MO.validate_scope(MO.SCOPE_WORKSPACE)[0] is True
