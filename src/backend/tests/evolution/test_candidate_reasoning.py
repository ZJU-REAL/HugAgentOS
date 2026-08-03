"""What a reviewer is shown, and whether it is true.

Three defects this pins, all of which made the review queue actively misleading
rather than merely sparse:

* the "why" column showed the attributor's **category label**, so every
  orchestration candidate read "过程停滞：重复动作、无产出增量" — a claim about
  stalling that nothing had measured — while the sentence describing what was
  actually observed sat unused;
* that label was generated from *invented* failure features, supplied only to
  make the failure attributor return the desired target for findings that are
  not failures at all;
* every cycle minted a fresh near-duplicate of the same proposal, because the
  hashed payload carried the evidence count.
"""

import pytest

from core.evolution import credit as C
from core.evolution.console_view import candidate_to_dict


class _Row:
    """Minimal stand-in for an EvolutionCandidate row."""

    def __init__(self, hypothesis, explanation):
        self.candidate_id = "cand-1"
        self.target_kind = "agent_profile"
        self.target_asset_id = "auto-chat"
        self.operation = "new"
        self.status = "draft"
        self.risk_tier = "medium"
        self.credit_decision = {"explanation": explanation, "selected": "workflow",
                                "confidence": 0.66}
        self.hypothesis = hypothesis
        self.proposer = "orchestration_engine"
        self.approved_by = None
        self.evidence_refs = ["ep1", "ep2"]
        self.created_at = None
        self.ir = {}
        self.scope = {}
        self.base_version_id = None
        self.reject_reason = None
        self.change_checksum = "x"


def test_the_review_queue_leads_with_what_was_observed():
    """The category is the same on every candidate sharing a target; the
    hypothesis is the only line that can inform the decision."""
    payload = candidate_to_dict(
        _Row("chat 类任务的 17 条执行中，8 个工具一次都没被用过", "过程停滞：重复动作、无产出增量")
    )
    assert payload["why"].startswith("chat 类任务的 17 条执行")
    # The category is still available — just not in the column read first.
    assert payload["attributed_to"] == "workflow"


def test_the_category_is_used_only_when_there_is_nothing_specific():
    payload = candidate_to_dict(_Row("", "过程停滞：重复动作、无产出增量"))
    assert payload["why"] == "过程停滞：重复动作、无产出增量"


# ── Aggregate findings are not failure attributions ─────────────────────────


def test_an_aggregate_finding_carries_its_own_reason():
    decision = C.aggregate_finding(
        target=C.T_ORCHESTRATION,
        explanation="chat 类任务的 17 条执行中，8 个工具一次都没被用过",
        confidence=C.confidence_from_evidence(17),
        evidence_count=17,
    )
    assert decision.explanation.startswith("chat 类任务")
    assert decision.verdict == C.VERDICT_AGGREGATE
    assert decision.may_produce_candidate is True


def test_an_aggregate_finding_is_distinguishable_from_a_failure_attribution():
    """They answer different questions and only one is about blame."""
    aggregate = C.aggregate_finding(
        target=C.T_ORCHESTRATION, explanation="x", confidence=0.7, evidence_count=10
    )
    failure = C.assign_credit(
        C.CreditFeatures(outcome_verdict="failed", outcome_confidence=0.8, loop_stagnation=True)
    )
    assert aggregate.verdict != failure.verdict
    assert aggregate.assigner_version != failure.assigner_version


def test_thin_evidence_cannot_authorise_a_candidate():
    """The same governance floor applies however the decision was reached."""
    decision = C.aggregate_finding(
        target=C.T_SKILL,
        explanation="x",
        confidence=C.confidence_from_evidence(0),
        evidence_count=0,
    )
    assert decision.may_produce_candidate is False


def test_confidence_grows_with_corroboration_but_never_reaches_certainty():
    assert C.confidence_from_evidence(3) < C.confidence_from_evidence(30)
    # The corpus only shows what was tried, never what would have worked instead.
    assert C.confidence_from_evidence(10_000) < 1.0


# ── Deduplication ───────────────────────────────────────────────────────────


def test_the_same_change_from_more_evidence_is_still_the_same_change():
    """Otherwise every nightly cycle files the same proposal again.

    Observed: two rows for one tool-convergence proposal, identical except that
    one had seen 15 episodes and the other 16.
    """
    from core.evolution.ir import EvolutionIR

    def ir_for(profile_version):
        return EvolutionIR(
            target_kind="agent_profile",
            target_asset_id="auto-chat",
            base_version="builtin",
            operation="new",
            hypothesis=f"从 {profile_version} 条执行中观察到",  # narrative differs
            changes=[{"profile": {"profile_id": "auto-chat"}, "tool_allowlist": ["a", "b"]}],
            evidence_refs=[f"ep{i}" for i in range(profile_version)],
        )

    assert ir_for(15).change_checksum() == ir_for(16).change_checksum()


def test_a_genuinely_different_change_gets_its_own_checksum():
    from core.evolution.ir import EvolutionIR

    def ir_for(tools):
        return EvolutionIR(
            target_kind="agent_profile",
            target_asset_id="auto-chat",
            base_version="builtin",
            operation="new",
            changes=[{"profile": {"profile_id": "auto-chat"}, "tool_allowlist": tools}],
        )

    assert ir_for(["a", "b"]).change_checksum() != ir_for(["a"]).change_checksum()


# ── Tool convergence: "never used" is absence of evidence ───────────────────


def _views(task_type, n, used_tools):
    return [
        {"episode_id": f"e{i}", "task_type": task_type, "verdict": "success",
         "tool_sequence": list(used_tools), "skills_opened": []}
        for i in range(n)
    ]


def test_the_catch_all_task_type_is_never_narrowed():
    """"chat" is where every unclassified request lands.

    It has no stable tool profile by construction, so the tools it "never uses"
    are the ones that week's traffic did not reach for. Observed on a 17-episode
    dev corpus: a proposal to remove database access, report export, site
    publishing and scheduled tasks from every conversation.
    """
    from core.evolution.orchestration_gen import converge_tools

    offered = [f"tool{i}" for i in range(12)]
    proposals = converge_tools(
        _views("chat", 400, ["tool0", "tool1"]), offered_tools={"chat": offered}
    )
    assert proposals == []


def test_a_thin_corpus_cannot_remove_a_capability():
    from core.evolution.orchestration_gen import converge_tools

    offered = [f"tool{i}" for i in range(12)]
    assert (
        converge_tools(_views("finance", 20, ["tool0", "tool1", "tool2"]),
                       offered_tools={"finance": offered})
        == []
    )


def test_a_cut_this_large_is_reported_but_not_proposed():
    """Removing most of the toolbox is a different product, not a tuning step."""
    from core.evolution.orchestration_gen import converge_tools

    offered = [f"tool{i}" for i in range(12)]
    # 400 episodes, only 3 tools ever used → 9 of 12 unused, far past the cap.
    assert (
        converge_tools(_views("finance", 400, ["tool0", "tool1", "tool2"]),
                       offered_tools={"finance": offered})
        == []
    )


def test_a_modest_well_evidenced_narrowing_is_proposed():
    from core.evolution.orchestration_gen import converge_tools

    offered = [f"tool{i}" for i in range(12)]
    used = [f"tool{i}" for i in range(9)]  # 3 of 12 unused — within the cap
    proposals = converge_tools(
        _views("finance", 400, used), offered_tools={"finance": offered}
    )
    assert len(proposals) == 1
    assert sorted(proposals[0].payload["dropped"]) == ["tool10", "tool11", "tool9"]


# ── Orchestration governs the ReAct loop, not a workflow ────────────────────


def test_the_attribution_target_names_the_assembly_not_a_dag():
    """The product's main axis is a ReAct loop, which executes no DAG.

    Labelling the responsible layer "workflow" pointed reviewers at a thing that
    does not exist here, on every orchestration candidate.
    """
    assert C.T_ORCHESTRATION == "orchestration"


def test_react_observable_signals_are_declared_separately():
    """A rule that can never fire looks the same, in the console, as one that
    simply has not fired yet."""
    from core.evolution import agent_profile as AP

    assert AP.SIGNAL_REPEATED_ACTIONS in AP.REACT_OBSERVABLE_SIGNALS
    assert AP.SIGNAL_TOOL_ERROR_STREAK in AP.REACT_OBSERVABLE_SIGNALS
    # Needs a file tree / a reviewer verdict: a single answer has neither.
    assert AP.SIGNAL_NO_DIFF in AP.LOOP_ONLY_SIGNALS
    assert AP.SIGNAL_REVIEWER_FLAT in AP.LOOP_ONLY_SIGNALS
    assert not (AP.REACT_OBSERVABLE_SIGNALS & AP.LOOP_ONLY_SIGNALS)


def test_the_builtin_profile_only_carries_rules_the_react_loop_can_act_on():
    """Shipping a default rule the main axis cannot observe would mean the
    built-in profile advertises an intervention that never happens."""
    from core.evolution import agent_profile as AP

    builtin = AP.builtin_profile()
    react_rules = [
        r for r in builtin.intervention_rules if r.signal in AP.REACT_OBSERVABLE_SIGNALS
    ]
    assert react_rules, "the default profile must be able to intervene on the main axis"
