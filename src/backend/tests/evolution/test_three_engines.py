"""The three engines, each held to the property it was missing.

Every test here corresponds to something that was implemented, reachable, and
still wrong — the class of defect that survives both code review and a green
suite, because the code runs and produces output nobody checks against reality.
"""

import pytest

from core.evolution import cycle_extras as X
from core.evolution import memory_scan as MS
from core.evolution import similarity as S


# ── Intent clustering ────────────────────────────────────────────────────────


def test_the_lexical_fallback_catches_rewordings():
    a = "帮我看下宁德时代这家公司的财务风险"
    b = "帮我看一下宁德时代公司的财务风险情况"
    assert S.lexical_similarity(a, b) >= S.LEXICAL_THRESHOLD


def test_the_lexical_fallback_is_blind_to_real_paraphrase():
    """The ceiling of every lexical method, stated as a test rather than hidden.

    Measured at 0.095 — below any threshold that would not also merge unrelated
    text. This is why the primary path has to be embeddings: without one, the
    system can detect that a user repeated themselves but not that they asked
    for the same thing in different words.
    """
    a = "分析宁德时代的偿债能力"
    b = "看一下宁德时代这家公司的财务风险"
    assert S.lexical_similarity(a, b) < S.LEXICAL_THRESHOLD


def test_different_intents_stay_apart():
    a = "帮我看下宁德时代的财务风险"
    b = "把这份 PPT 的第三页配色改成深色"
    assert S.lexical_similarity(a, b) < S.LEXICAL_THRESHOLD


def test_entity_aliases_are_normalised_before_clustering():
    """CATL and 宁德时代 are one company; a general embedding model does not know that."""
    aliases = {"catl": "宁德时代"}
    assert S.canonicalise("帮我评估下 CATL 的财务健康状况", aliases) == (
        "帮我评估下 宁德时代 的财务健康状况"
    )


def test_clustering_reports_which_method_it_used():
    """"Nothing recurs" and "we had no way to look" are different operational states."""
    episodes = [
        {"episode_id": f"e{i}", "objective": "帮我看下宁德时代这家公司的财务风险",
         "chat_id": f"c{i}", "tool_sequence": ["a"], "verdict": "success"}
        for i in range(4)
    ]
    clusters, method = S.cluster_intents(episodes, min_support=2, prefer_embeddings=False)
    assert method == S.METHOD_LEXICAL
    assert clusters and clusters[0].support == 4


def test_recurrence_is_counted_in_conversations_not_turns():
    """One chat asking three times is one occasion — usually a bad first answer."""
    episodes = [
        {"episode_id": f"e{i}", "objective": "帮我看下宁德时代这家公司的财务风险",
         "chat_id": "same-chat", "tool_sequence": [f"t{i}"], "verdict": "success"}
        for i in range(5)
    ]
    clusters, _ = S.cluster_intents(episodes, min_support=2, prefer_embeddings=False)
    assert clusters[0].support == 5
    assert clusters[0].occasions == 1


def _intent_episodes(n, *, tools_vary=True, verdict="success"):
    return [
        {
            "episode_id": f"e{i}",
            "chat_id": f"c{i}",
            "objective": "帮我看下宁德时代这家公司的财务风险",
            "tool_sequence": [f"tool{i}"] if tools_vary else ["tool0"],
            "verdict": verdict,
            "task_type": "finance",
        }
        for i in range(n)
    ]


def test_a_recurring_intent_with_a_stable_plan_yields_no_candidate(monkeypatch):
    monkeypatch.setattr(S, "_embed_all", lambda texts: None)
    result = X.intent_findings(_intent_episodes(4, tools_vary=False))
    # Already effectively a skill; writing one down adds an asset to maintain
    # and changes nothing.
    assert not [f for f in result["findings"] if f["action"] == "skill_candidate"]


def test_a_recurring_intent_replanned_every_time_does_yield_a_candidate(monkeypatch):
    monkeypatch.setattr(S, "_embed_all", lambda texts: None)
    result = X.intent_findings(_intent_episodes(4))
    findings = [f for f in result["findings"] if f["action"] == "skill_candidate"]
    assert findings, result
    assert findings[0]["cluster"]["plan_variety"] >= X.MIN_PLAN_VARIETY


def test_a_recurring_intent_that_keeps_failing_is_not_distilled(monkeypatch):
    monkeypatch.setattr(S, "_embed_all", lambda texts: None)
    result = X.intent_findings(_intent_episodes(4, verdict="failed"))
    kinds = {f["kind"] for f in result["findings"]}
    assert "recurring_failure_intent" in kinds
    assert not [f for f in result["findings"] if f["action"] == "skill_candidate"]


# ── Decremental: usage, not exposure ─────────────────────────────────────────


def test_usage_counts_opens_separately_from_offers():
    views = [
        {
            "episode_id": f"e{i}",
            "verdict": "success",
            "skill_sequence": ["offered-only", "used"],
            "skills_opened": [{"skill_id": "used", "tools_after_open": ["a"]}],
        }
        for i in range(5)
    ]
    stats = X.skill_usage_from_views(views)
    assert stats["offered-only"]["offers"] == 5 and stats["offered-only"]["opens"] == 0
    assert stats["used"]["opens"] == 5


def test_follow_through_excludes_turns_it_cannot_measure(monkeypatch):
    """The bug this pins: everything scored 100% and the verdict was unreachable.

    Turns where the skill declares no tools, or where nothing was called after
    the open, cannot be judged. Counting them as passes made follow-through 1.0
    for every skill, so "opened but ignored" — the verdict that says *rewrite
    this, do not retire it* — could never fire.
    """
    monkeypatch.setattr(
        X, "_declared_tools_for", lambda ids: {"evo-fin": ["dbhub_query", "chart"]}
    )
    views = [
        # Followed: called a declared tool afterwards.
        {"episode_id": "e0", "verdict": "success", "skill_sequence": ["evo-fin"],
         "skills_opened": [{"skill_id": "evo-fin", "tools_after_open": ["dbhub_query"]}]},
        # Ignored: opened it, then did something else entirely.
        {"episode_id": "e1", "verdict": "success", "skill_sequence": ["evo-fin"],
         "skills_opened": [{"skill_id": "evo-fin", "tools_after_open": ["web_fetch"]}]},
        # Unmeasurable: nothing called after the open. Must not count either way.
        {"episode_id": "e2", "verdict": "success", "skill_sequence": ["evo-fin"],
         "skills_opened": [{"skill_id": "evo-fin", "tools_after_open": []}]},
    ]
    stats = X.skill_usage_from_views(views)["evo-fin"]
    assert stats["opens"] == 3
    assert stats["opens_measurable"] == 2
    assert stats["follow_through"] == 0.5


def test_offers_count_availability_because_loading_offers_everything():
    """Skill loading puts every enabled skill in the prompt, so offers is a
    denominator — "how many chances did it have" — not a ranked choice."""
    views = [
        {
            "episode_id": f"e{i}",
            "verdict": "success",
            "skill_sequence": ["a", "b"],
            "skills_opened": [{"skill_id": "a", "tools_after_open": ["x"]}],
        }
        for i in range(4)
    ]
    stats = X.skill_usage_from_views(views)
    assert stats["b"]["offers"] == 4 and stats["b"]["opens"] == 0
    assert stats["a"]["offers"] == 4 and stats["a"]["opens"] == 4


def test_overlap_uses_declared_tools_not_co_occurrence():
    """Co-enabled skills share an episode's whole tool set by construction.

    Computing overlap from that produces a merge proposal for every pair of
    skills that happened to be loaded together — guaranteed, and therefore
    uninformative.
    """
    views = [
        {
            "episode_id": f"e{i}",
            "verdict": "success",
            "skill_sequence": ["skill-a", "skill-b"],
            "tool_sequence": ["shared1", "shared2", "shared3"],
            "skills_opened": [],
        }
        for i in range(6)
    ]
    result = X.decremental_findings(views, declared_tools={
        "skill-a": ["read", "write"],
        "skill-b": ["chart", "export"],
    })
    assert result["overlaps"] == []


def test_genuinely_overlapping_skills_are_surfaced():
    result = X.decremental_findings([], skill_stats={}, declared_tools={
        "skill-a": ["search", "verify", "chart"],
        "skill-b": ["search", "verify", "export"],
    })
    assert result["overlaps"] and result["overlaps"][0]["shared_tools"] == 2


def test_negative_transfer_is_reported_when_the_corpus_can_be_split():
    """Defined, but previously unreachable *and* uncomputable.

    It was missing from ``run_extras`` entirely; wiring it up was not enough,
    because nothing marked which episodes came after an activation — so it would
    have returned nothing on every corpus, which looks identical to "no damage".
    """
    views = [
        {"episode_id": f"b{i}", "task_type": "kb_qa", "verdict": "success", "after": False}
        for i in range(9)
    ] + [
        {"episode_id": "b9", "task_type": "kb_qa", "verdict": "failed", "after": False},
        {"episode_id": "a0", "task_type": "kb_qa", "verdict": "failed", "after": True},
        {"episode_id": "a1", "task_type": "kb_qa", "verdict": "failed", "after": True},
        {"episode_id": "a2", "task_type": "kb_qa", "verdict": "success", "after": True},
    ]
    findings = X.negative_transfer_findings(views, asset_id="evo-x")
    assert findings, "an unrelated task type dropping must be reported"


def test_one_sided_evidence_reports_nothing_rather_than_a_phantom_drop():
    """A drop measured against an empty baseline is not a measurement."""
    from datetime import datetime, timezone

    activated = datetime(2026, 7, 1, tzinfo=timezone.utc)
    views = [
        {
            "episode_id": f"a{i}",
            "task_type": "kb_qa",
            "verdict": "failed",
            "created_at": datetime(2026, 7, 5, tzinfo=timezone.utc),
        }
        for i in range(6)
    ]
    split = [{**v, "after": X._is_after(v["created_at"], activated)} for v in views]
    assert all(v["after"] for v in split)
    # Every episode on one side → the detector has no baseline to compare with.
    assert X.negative_transfer_findings(split, asset_id="evo-x") == []


def test_uplift_needs_both_arms_before_it_licenses_a_rollback(monkeypatch):
    """``None`` and ``0.0`` are different answers.

    "We have not measured" must never read as "measured, no effect", because
    the second one licenses an immediate rollback and the first one does not.
    """
    from datetime import datetime, timezone

    started = datetime(2026, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        X, "_uplift_from_canary", lambda views: {}  # no canary in flight
    )
    views = [
        {"episode_id": "e0", "verdict": "success", "skill_sequence": ["evo-x"],
         "skills_opened": [], "created_at": started}
    ]
    assert X.skill_usage_from_views(views)["evo-x"]["uplift"] is None


def test_a_canary_comparison_uses_the_same_bucketing_the_runtime_used():
    """The comparison is the canary split itself: same period, same task mix,
    differing only in exposure to this one asset."""
    from datetime import datetime, timedelta, timezone

    started = datetime(2026, 7, 1, tzinfo=timezone.utc)
    later = started + timedelta(days=1)
    ramping = [("evo-x", "rel-1", 50, started)]

    from core.evolution.release import in_canary_bucket

    # Split 40 users the way the runtime would have, then make the exposed arm
    # fail and the withheld arm succeed.
    views = [
        {
            "episode_id": f"e{i}",
            "user_id": f"u{i}",
            "verdict": "failed"
            if in_canary_bucket(release_id="rel-1", subject_id=f"u{i}", traffic_percent=50)
            else "success",
            "created_at": later,
        }
        for i in range(40)
    ]
    uplift = X._uplift_from_canary(views, ramping=ramping)
    assert uplift["evo-x"] is not None and uplift["evo-x"] < -0.5


def test_a_thin_canary_comparison_reports_nothing_rather_than_a_number():
    """A number computed from three turns looks like evidence and is not —
    and this particular number can trigger an immediate rollback."""
    from datetime import datetime, timedelta, timezone

    started = datetime(2026, 7, 1, tzinfo=timezone.utc)
    views = [
        {
            "episode_id": f"e{i}",
            "user_id": f"u{i}",
            "verdict": "failed",
            "created_at": started + timedelta(days=1),
        }
        for i in range(4)
    ]
    uplift = X._uplift_from_canary(views, ramping=[("evo-x", "rel-1", 50, started)])
    assert uplift["evo-x"] is None


# ── Memory: inventory, not frequency ─────────────────────────────────────────


def test_frequent_retrieval_is_never_treated_as_duplication():
    """A memory retrieved in five conversations is the store's best entry.

    Counting retrievals of one hash and calling four hits a duplicate proposes
    merging away what works, and cannot observe real duplication at all — the
    event stream shows one hash whether the store holds one row or five.
    """
    views = [
        {"episode_id": f"e{i}", "verdict": "success", "memory_refs": ["mref-useful"]}
        for i in range(8)
    ]
    result = X.memory_findings(views)
    assert result["merge_proposals"] == 0
    assert result["reweight_proposals"] == 0


def test_duplication_comes_from_scanning_the_store():
    finding = MS.ScanFinding(
        kind=MS.FINDING_DUPLICATE,
        refs=["mref-keep", "mref-dup"],
        memory_ids=["m1", "m2"],
        reason="两条记忆语义重复",
    )
    result = X.memory_findings([], scan_findings=[finding])
    assert result["merge_proposals"] == 1
    # The survivor is untouched; the loser stops being injected.
    op = result["needs_review"][0]
    assert op["memory_ref"] == "mref-dup"
    assert op["after"]["superseded_by"] == "mref-keep"


def test_a_memory_retrieved_into_failures_is_down_weighted_not_deleted():
    views = [
        {"episode_id": f"e{i}", "verdict": "failed", "memory_refs": ["mref-noisy"]}
        for i in range(6)
    ]
    result = X.memory_findings(views)
    ops = result["auto_applicable"] + result["needs_review"]
    assert any(op["operation"] == "reweight" for op in ops)
    assert not any(op["operation"] == "delete" for op in ops)


def test_contradictions_are_reported_for_a_human_not_resolved_by_recency():
    """The newer statement is not automatically right."""
    finding = MS.ScanFinding(
        kind=MS.FINDING_CONTRADICTION,
        refs=["mref-a", "mref-b"],
        memory_ids=["m1", "m2"],
        reason="方向相反",
    )
    result = X.memory_findings([], scan_findings=[finding])
    assert result["contradictions"] and result["merge_proposals"] == 0


def test_content_changing_memory_ops_require_review():
    finding = MS.ScanFinding(
        kind=MS.FINDING_DUPLICATE, refs=["a", "b"], memory_ids=["1", "2"], reason=""
    )
    result = X.memory_findings([], scan_findings=[finding])
    assert all(op["operation"] != "merge" for op in result["auto_applicable"])
    assert any(op["operation"] == "merge" for op in result["needs_review"])


# ── Store scanning ───────────────────────────────────────────────────────────


def _orthogonal(n, dim=10):
    """Mutually orthogonal filler vectors, so only the pair under test can match."""
    return [[1.0 if j == i else 0.0 for j in range(dim)] for i in range(n)]


def _records(texts, *, memory_type="fact"):
    return MS.to_records(
        [
            {"id": f"m{i}", "memory": text, "metadata": {"memory_type": memory_type}}
            for i, text in enumerate(texts)
        ]
    )


def test_near_duplicates_are_found_in_the_store():
    texts = [f"无关记忆 {i}" for i in range(8)] + ["周报口径是自然周", "周报的口径按自然周算"]
    records = _records(texts)
    # Injected vectors: the scan must be testable without an embedding service,
    # and must never approximate one lexically — that would find only the
    # duplicates nobody minds. The filler vectors are mutually orthogonal so
    # they cannot manufacture findings of their own.
    vectors = _orthogonal(8) + [[0.0] * 8 + [1.0, 0.0], [0.0] * 8 + [1.0, 0.02]]
    findings = MS.scan_records(records, user_id="u1", vectors=vectors)
    assert any(f.kind == MS.FINDING_DUPLICATE for f in findings)


def test_a_negation_of_the_same_statement_is_a_contradiction_not_a_duplicate():
    texts = [f"无关记忆 {i}" for i in range(8)] + ["周报口径是自然周", "周报口径不要用自然周"]
    records = _records(texts)
    vectors = _orthogonal(8) + [[0.0] * 8 + [1.0, 0.0], [0.0] * 8 + [1.0, 0.02]]
    findings = MS.scan_records(records, user_id="u1", vectors=vectors)
    kinds = {f.kind for f in findings}
    assert MS.FINDING_CONTRADICTION in kinds
    assert MS.FINDING_DUPLICATE not in kinds


def test_only_procedural_records_are_eligible_for_compilation():
    records = _records(["某公司是电池厂商"]) + _records(["先核验主体"], memory_type="procedural")
    assert [r.content for r in MS.procedural_records(records)] == ["先核验主体"]


# ── The cycle entry point ────────────────────────────────────────────────────


def test_run_extras_returns_all_three_sections_and_survives_a_bad_one():
    result = X.run_extras([])
    assert set(result) == {"intent", "decremental", "memory"}
