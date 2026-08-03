"""GCE ticket 10 — skill selection evidence.

The point of this ticket is not the token saving; it is that "never selected"
and "selected but unhelpful" become distinguishable. Those two demand opposite
fixes — better routing versus a better skill — so skill attribution cannot start
until the record exists.
"""

from core.agent_skills import selection_record as SR


def test_rejected_candidates_are_recorded_with_a_reason():
    selection = SR.build_selection(
        all_candidate_ids=["a", "b", "c"], selected_ids=["a"], scores={"a": 0.9, "b": 0.4}
    )
    rejected = {c.skill_id: c for c in selection.candidates if not c.selected}
    assert set(rejected) == {"b", "c"}
    # "b" was scored but lost the ranking; "c" was never in contention. The
    # distinction is exactly the negative signal attribution needs.
    assert rejected["b"].reason == SR.REASON_RANKED_OUT
    assert rejected["c"].reason == SR.REASON_NOT_RELEVANT


def test_selected_candidates_carry_rank():
    selection = SR.build_selection(
        all_candidate_ids=["a", "b"], selected_ids=["b", "a"]
    )
    ranks = {c.skill_id: c.rank for c in selection.candidates if c.selected}
    assert ranks == {"b": 1, "a": 2}


def test_event_payload_surfaces_rejections_explicitly():
    payload = SR.build_selection(
        all_candidate_ids=["a", "b", "c"], selected_ids=["a"]
    ).to_event_payload()
    assert payload["selected"] == ["a"]
    assert sorted(payload["rejected"]) == ["b", "c"]


def test_duplicate_selections_are_collapsed():
    selection = SR.build_selection(all_candidate_ids=["a"], selected_ids=["a", "a"])
    assert selection.selected_ids == ["a"]
    assert len([c for c in selection.candidates if c.selected]) == 1


# ── Degradation: routing must never fail a turn ──────────────────────────────


def test_selector_failure_registers_every_candidate():
    class Boom:
        pass

    def explode(**_kw):
        raise RuntimeError("model down")

    import core.agent_skills.selector as selector_mod

    original = selector_mod.select_skills_for_query
    selector_mod.select_skills_for_query = explode
    try:
        selection = SR.select_with_record(
            user_query="做个周报",
            available_skill_ids=["a", "b"],
            enabled_skill_ids=["a", "b"],
            model=Boom(),
        )
    finally:
        selector_mod.select_skills_for_query = original

    # Losing the token saving is annoying; failing the user's turn because a
    # routing optimisation broke would be inexcusable.
    assert selection.degraded is True
    assert sorted(selection.selected_ids) == ["a", "b"]
    assert all(c.reason == SR.REASON_SELECTOR_UNAVAILABLE for c in selection.candidates if not c.selected)


def test_no_model_keeps_everything_rather_than_guessing():
    selection = SR.select_with_record(
        user_query="做个周报",
        available_skill_ids=["a", "b"],
        enabled_skill_ids=["a", "b"],
        model=None,
    )
    assert selection.strategy == "passthrough"
    assert sorted(selection.selected_ids) == ["a", "b"]


def test_empty_candidate_set_is_not_an_error():
    selection = SR.select_with_record(
        user_query="x", available_skill_ids=[], enabled_skill_ids=["a"], model=object()
    )
    assert selection.selected_ids == []
    assert selection.candidates == []


def test_selection_is_intersected_with_enabled_set():
    """A skill the catalog disabled must not slip back in through selection."""
    selection = SR.build_selection(
        all_candidate_ids=["a"], selected_ids=["a"], scores={}
    )
    assert selection.selected_ids == ["a"]

    import core.agent_skills.selector as selector_mod

    original = selector_mod.select_skills_for_query
    selector_mod.select_skills_for_query = lambda **_kw: ["a", "not-enabled"]
    try:
        result = SR.select_with_record(
            user_query="q",
            available_skill_ids=["a", "not-enabled"],
            enabled_skill_ids=["a"],
            model=object(),
        )
    finally:
        selector_mod.select_skills_for_query = original
    assert result.selected_ids == ["a"]
