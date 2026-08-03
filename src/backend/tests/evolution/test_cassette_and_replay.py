"""GCE tickets 15 / 16 — cassettes and the replay engine.

The load-bearing assertions: a replay never performs an external write, an
unfaithfully-reconstructable recording is a miss rather than an approximation,
and the comparison is paired (an unpaired test at these sample sizes could not
see the effects the design cares about).
"""

import pytest

from core.evolution import cassette as CA
from core.evolution import replay as RP


# ── Cassette: side-effect safety ─────────────────────────────────────────────


def test_write_tools_are_refused_not_replayed():
    gate = CA.ReplayToolGate(CA.Cassette())
    for tool in ("write_file", "edit_file", "bash", "sandbox_put_artifact", "delete_row"):
        served, result, reason = gate.call(tool, {"path": "/tmp/x"})
        # Refusing beats dry-running: a "dry run" of an unclassified tool is an
        # assumption, whereas refusal keeps "replay performs no writes" checkable.
        assert served is False and result is None
        assert reason == CA.MISS_WRITE_TOOL
    assert len(gate.refused_writes) == 5


def test_read_tools_are_served_from_the_recording():
    cassette = CA.Cassette()
    cassette.add(
        CA.CassetteEntry(
            tool_name="kb_search",
            args_hash=CA.args_hash({"q": "锂盐价格"}),
            result={"hits": 3},
        )
    )
    gate = CA.ReplayToolGate(cassette)
    served, result, reason = gate.call("kb_search", {"q": "锂盐价格"})
    assert served is True and result == {"hits": 3} and reason == ""
    assert gate.served == 1


def test_unknown_tools_default_to_write_and_are_refused():
    """An allow-list, because the dangerous case is the tool nobody classified."""
    gate = CA.ReplayToolGate(CA.Cassette())
    served, _, reason = gate.call("some_new_integration", {})
    assert served is False and reason == CA.MISS_WRITE_TOOL


def test_missing_recording_is_a_miss_not_a_live_call():
    gate = CA.ReplayToolGate(CA.Cassette())
    served, result, reason = gate.call("kb_search", {"q": "never recorded"})
    assert served is False and result is None
    assert reason == CA.MISS_NOT_RECORDED


# ── Cassette: argument hashing ───────────────────────────────────────────────


def test_argument_order_does_not_change_the_key():
    # The model reorders keys freely between runs; that must not read as a
    # different call.
    assert CA.args_hash({"a": 1, "b": 2}) == CA.args_hash({"b": 2, "a": 1})


def test_different_arguments_produce_different_keys():
    assert CA.args_hash({"q": "a"}) != CA.args_hash({"q": "b"})


def test_unserialisable_arguments_still_hash():
    class Weird:
        pass

    assert CA.args_hash({"obj": Weird()})


# ── Cassette: the two silent-corruption traps ────────────────────────────────


class _Row:
    def __init__(self, **kw):
        self.tool_name = kw.get("tool_name", "kb_search")
        self.tool_args = kw.get("tool_args", {"q": "x"})
        self.tool_result = kw.get("tool_result")
        self.result_truncated = kw.get("result_truncated", False)
        self.status = "success"
        self.duration_ms = 10


def test_truncated_result_that_cannot_be_restored_is_dropped():
    """Replaying a clipped result would silently change what the agent saw."""
    result, failure = CA._recover_result(
        _Row(tool_result="clipped...", result_truncated=True)
    )
    assert result is None
    assert failure == CA.MISS_TRUNCATED


def test_offloaded_result_that_cannot_be_restored_is_dropped():
    result, failure = CA._recover_result(
        _Row(tool_result={"_offload_ref": "storage://missing"})
    )
    assert result is None
    assert failure == CA.MISS_OFFLOADED


def test_inline_result_passes_through_unchanged():
    result, failure = CA._recover_result(_Row(tool_result={"hits": 7}))
    assert result == {"hits": 7} and failure is None


def test_coverage_is_reported_so_degradation_is_visible():
    cassette = CA.Cassette()
    cassette.add(CA.CassetteEntry("kb_search", CA.args_hash({"q": "a"}), {"ok": 1}))
    cassette.lookup("kb_search", {"q": "a"})
    cassette.lookup("kb_search", {"q": "missing"})
    report = cassette.report()
    assert report["entries"] == 1 and report["misses"] == 1
    assert 0.0 < report["coverage"] < 1.0


# ── Replay: paired statistics ────────────────────────────────────────────────


def _arm(task_successes):
    return [
        RP.TaskResult(task_id=f"t{i}", success=ok, cost_usd=0.01, latency_ms=100)
        for i, ok in enumerate(task_successes)
    ]


def test_paired_comparison_uses_only_discordant_pairs():
    # 30 tasks: 10 both-right, 10 both-wrong, 10 candidate-only-right.
    base = _arm([True] * 10 + [False] * 10 + [False] * 10)
    cand = _arm([True] * 10 + [False] * 10 + [True] * 10)
    report = RP.compare_paired(base, cand)
    # Tasks both arms agreed on carry no information about the difference.
    assert report.discordant_pairs == (0, 10)
    assert report.verdict == RP.VERDICT_IMPROVED
    assert report.effect_size > 0


def test_a_regression_is_detected():
    base = _arm([True] * 20 + [True] * 10)
    cand = _arm([True] * 20 + [False] * 10)
    report = RP.compare_paired(base, cand)
    assert report.verdict == RP.VERDICT_REGRESSED


def test_small_samples_withhold_an_improvement_claim():
    """A large positive effect on six tasks is noise, not a finding."""
    base = _arm([False] * 6)
    cand = _arm([True] * 6)
    report = RP.compare_paired(base, cand)
    assert report.verdict == RP.VERDICT_INSUFFICIENT
    assert report.sample_size < RP.MIN_SAMPLE


def test_small_samples_still_report_a_significant_regression():
    """The floor is asymmetric on purpose.

    Withholding "proven better" on thin evidence is prudent; withholding
    "clearly worse" would defeat the sentinel pre-screen, whose entire job is to
    catch broken candidates before the full set is paid for.
    """
    base = _arm([True] * 8)
    cand = _arm([False] * 8)
    report = RP.compare_paired(base, cand)
    assert report.verdict == RP.VERDICT_REGRESSED
    assert any("defeat the pre-screen" in note for note in report.notes)


def test_a_tiny_effect_on_a_large_sample_is_neutral():
    base = _arm([True] * 49 + [False])
    cand = _arm([True] * 50)
    report = RP.compare_paired(base, cand)
    assert report.verdict == RP.VERDICT_NEUTRAL


def test_increased_risk_denials_disqualify_regardless_of_quality():
    """Answering better while tripping the gate more often is not an improvement."""
    base = [RP.TaskResult(f"t{i}", success=False) for i in range(30)]
    cand = [
        RP.TaskResult(f"t{i}", success=True, risk_denied=(i < 5)) for i in range(30)
    ]
    report = RP.compare_paired(base, cand)
    assert report.verdict == RP.VERDICT_REGRESSED
    assert "risk_denials_increased" in report.guardrail_breaches


def test_non_overlapping_task_sets_produce_no_verdict():
    base = [RP.TaskResult("a", True)]
    cand = [RP.TaskResult("b", True)]
    report = RP.compare_paired(base, cand)
    assert report.sample_size == 0
    assert report.verdict == RP.VERDICT_INSUFFICIENT


def test_dataset_snapshot_is_recorded_for_reproducibility():
    report = RP.compare_paired(_arm([True] * 25), _arm([True] * 25))
    assert len(report.dataset_snapshot) == 25


# ── McNemar ──────────────────────────────────────────────────────────────────


def test_mcnemar_no_discordance_is_not_significant():
    assert RP.mcnemar_p_value(0, 0) == 1.0


def test_mcnemar_becomes_significant_with_one_sided_discordance():
    assert RP.mcnemar_p_value(0, 10) < 0.05
    assert RP.mcnemar_p_value(5, 5) > 0.05


def test_mcnemar_is_symmetric():
    assert RP.mcnemar_p_value(3, 9) == RP.mcnemar_p_value(9, 3)


# ── Multiple comparisons ─────────────────────────────────────────────────────


def test_bh_correction_rejects_borderline_results_among_mostly_null_candidates():
    """The realistic batch: one genuine improvement among many non-improvements.

    Without the correction, the borderline members of such a batch drift past an
    uncorrected 0.05 threshold and the system appears to be learning when it is
    only testing more often.
    """
    p_values = [0.001, 0.049, 0.30, 0.44, 0.61, 0.72]
    survives = RP.benjamini_hochberg(p_values, alpha=0.05)
    assert survives[0] is True
    # 0.049 clears a naive 0.05 cut-off but not the FDR-adjusted one.
    assert survives[1] is False
    assert sum(survives) == 1


def test_bh_keeps_clearly_significant_results():
    survives = RP.benjamini_hochberg([0.0001, 0.0002, 0.0003], alpha=0.05)
    assert all(survives)


def test_bh_on_empty_input():
    assert RP.benjamini_hochberg([]) == []


# ── Tiered replay (cost control) ─────────────────────────────────────────────


def test_broken_candidate_is_stopped_at_the_sentinel():
    def run_arm(task_id, is_candidate):
        return RP.TaskResult(task_id, success=not is_candidate)

    result = RP.run_tiered_replay([f"t{i}" for i in range(100)], run_arm, sentinel_size=8)
    assert result.passed_sentinel is False
    # 8 tasks × 2 arms — the full 200-run set was never paid for.
    assert result.tasks_run == 16


def test_neutral_sentinel_still_proceeds_to_the_full_set():
    """Eight tasks cannot show a five-point effect; failing there would discard
    nearly every genuine improvement."""

    def run_arm(task_id, is_candidate):
        index = int(task_id[1:])
        return RP.TaskResult(task_id, success=is_candidate or index % 2 == 0)

    result = RP.run_tiered_replay([f"t{i}" for i in range(40)], run_arm, sentinel_size=8)
    assert result.passed_sentinel is True
    assert result.report is not None
    assert result.tasks_run == 16 + 80


def test_empty_task_set_runs_nothing():
    result = RP.run_tiered_replay([], lambda t, c: None)
    assert result.tasks_run == 0 and result.passed_sentinel is False


# ── Cost projection ──────────────────────────────────────────────────────────


def test_cost_estimate_shows_how_fast_replay_grows():
    estimate = RP.estimate_cost(
        task_count=50, candidates=20, tokens_per_task=30_000, usd_per_million=3.0
    )
    # 50 × 2 arms × 20 candidates × 30k = 60M tokens a day.
    assert estimate["total_tokens"] == 60_000_000
    assert estimate["usd"] > 100
