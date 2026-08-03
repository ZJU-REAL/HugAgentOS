"""Benchmark harness, context-scoped ordering rules, and materialisation.

These cover the mechanism the before/after benchmark exposed: the loop produced
candidates but nothing reached the runtime, and once it did, transfer was
blocked by an over-broad conflict veto.
"""

import pytest

from core.evolution import benchmark as B
from core.evolution import promotion as P


# ── Context-scoped ordering constraints ──────────────────────────────────────


def _episodes():
    return (
        [
            {
                "verdict": "success",
                "task_type": "industry_report",
                "tool_sequence": ["company_verify", "db_query", "chart_render", "doc_export"],
            }
        ]
        * 8
        + [
            {
                "verdict": "success",
                "task_type": "finance",
                "tool_sequence": ["company_verify", "db_query", "risk_score"],
            }
        ]
        * 6
        + [
            {
                "verdict": "success",
                "task_type": "trap",
                "tool_sequence": ["db_query", "company_verify", "doc_export"],
            }
        ]
        * 4
    )


def _rule(constraints, before, after):
    return next(
        (c for c in constraints if c.before == before and c.after == after), None
    )


def test_a_rule_contradicted_elsewhere_is_scoped_rather_than_discarded():
    """A global veto threw away rules that were valid where they were observed."""
    rule = _rule(P.extract_ordering_constraints(_episodes()), "company_verify", "db_query")
    assert rule is not None
    assert set(rule.validated_in) == {"industry_report", "finance"}
    assert "trap" in rule.contradicted_in


def test_a_scoped_rule_applies_only_where_it_was_validated():
    rule = _rule(P.extract_ordering_constraints(_episodes()), "company_verify", "db_query")
    tools = ["company_verify", "db_query", "risk_score"]
    assert rule.applies_to(tools, "finance") is True
    assert rule.applies_to(["company_verify", "db_query", "doc_export"], "trap") is False


def test_a_rule_known_to_flip_is_not_extrapolated_to_an_unseen_context():
    """Guessing is exactly what a rule with a known exception must not do."""
    rule = _rule(P.extract_ordering_constraints(_episodes()), "company_verify", "db_query")
    assert rule.applies_to(["company_verify", "db_query"], "never_seen_before") is False


def test_an_uncontradicted_rule_does_extend_to_unseen_contexts():
    episodes = [
        {
            "verdict": "success",
            "task_type": f"type-{i % 3}",
            "tool_sequence": ["a", "b", "c"],
        }
        for i in range(9)
    ]
    rule = _rule(P.extract_ordering_constraints(episodes, min_contexts=1), "a", "b")
    assert rule is not None and rule.contradicted_in == ()
    assert rule.applies_to(["a", "b"], "brand_new_type") is True


def test_a_pair_that_flips_within_one_task_type_is_dropped_for_that_type():
    """No context resolves it, so majority-voting would just break the minority."""
    episodes = (
        [{"verdict": "success", "task_type": "mixed", "tool_sequence": ["a", "b"]}] * 6
        + [{"verdict": "success", "task_type": "mixed", "tool_sequence": ["b", "a"]}] * 6
    )
    constraints = P.extract_ordering_constraints(episodes, min_contexts=1)
    assert _rule(constraints, "a", "b") is None
    assert _rule(constraints, "b", "a") is None


def test_failed_episodes_are_not_treated_as_evidence():
    episodes = [
        {"verdict": "failed", "task_type": "t", "tool_sequence": ["a", "b"]}
    ] * 20
    assert P.extract_ordering_constraints(episodes, min_contexts=1) == []


def test_constraints_reorder_only_what_they_constrain():
    rule = P.OrderingConstraint(before="verify", after="query", support=9, contexts=2)
    ordered = P.apply_ordering_constraints(["query", "chart", "verify", "export"], [rule])
    assert ordered.index("verify") < ordered.index("query")
    # Unconstrained pairs keep their relative order — applying one rule must not
    # silently reshuffle everything else.
    assert ordered.index("chart") < ordered.index("export")


# ── Benchmark harness integrity ──────────────────────────────────────────────


def test_task_set_includes_distractors_and_a_trap():
    families = {t.family for t in B.build_task_set()}
    # Without these the benchmark could only ever succeed.
    assert "kb_qa" in families
    assert "trap" in families


def test_the_trap_has_passes_to_lose():
    """A trap whose tasks all fail anyway cannot detect over-generalisation."""
    summary = B.ArmSummary("base", B.run_arm(B.build_task_set(), {}))
    trap = summary.by_family()["trap"]
    assert 0 < trap["passed"] < trap["total"]


def test_a_memorised_sequence_does_not_claim_a_subset_task():
    """The over-generalisation the trap caught: subset matching imposed a
    skill's ordering on tasks where the opposite order is correct."""
    tasks = [t for t in B.build_task_set() if t.family == "trap"]
    skills = {"evo-report": ["company_verify", "db_query", "chart_render", "doc_export"]}
    results = B.run_arm(tasks, skills)
    assert all(r.used_skill is None for r in results)


def test_an_exact_tool_set_match_does_use_the_sequence():
    tasks = [t for t in B.build_task_set() if t.family == "industry_report"]
    skills = {"evo-report": ["company_verify", "db_query", "chart_render", "doc_export"]}
    results = B.run_arm(tasks, skills)
    assert all(r.used_skill == "evo-report" and r.success for r in results)


def test_rules_earn_credit_only_by_correcting_a_wrong_plan():
    """Applying rules to the declared tool list flattered them: that list was
    already correctly ordered, so a rule scored a pass without doing work."""
    tasks = [t for t in B.build_task_set() if t.family == "finance"]
    unhelpful = P.OrderingConstraint(
        before="risk_score", after="nonexistent", support=9, contexts=2
    )
    results = B.run_arm(tasks, {}, ordering_rules=[unhelpful])
    assert all(r.used_skill is None for r in results)


def test_comparison_flags_distractor_contamination():
    tasks = B.build_task_set()
    before = B.ArmSummary("before", B.run_arm(tasks, {}))
    after = B.ArmSummary("after", list(before.results))
    # Hand-alter a distractor result: the comparison must call the run invalid.
    for index, result in enumerate(after.results):
        if result.family == "kb_qa":
            after.results[index] = B.RunResult(
                task_id=result.task_id,
                family=result.family,
                success=not result.success,
                order=result.order,
            )
            break
    verdict = B.compare(before, after)
    assert verdict["distractor_contaminated"] is True
    assert verdict["valid"] is False


def test_comparison_reports_regressions_explicitly():
    tasks = B.build_task_set()
    before = B.ArmSummary("before", B.run_arm(tasks, {}))
    after = B.ArmSummary(
        "after",
        [
            B.RunResult(r.task_id, r.family, False, r.order)
            if r.family == "trap"
            else r
            for r in before.results
        ],
    )
    verdict = B.compare(before, after)
    assert verdict["regressed_tasks"] > 0
    assert verdict["regressions"]
