"""Per-turn settlement: the card reports memory, and nothing else.

The rule this file defends is that a turn can only claim what it changed. Skills
and orchestration policies are batch products — a turn cannot create one, and
the ones it used were already installed — so they never appear here. What does
appear is every memory the turn wrote, each carrying the handle the user needs
to edit or delete it.
"""

from core.evolution import settlement as S


def _entry(handle="m-1", text="做财务分析前先核验主体", layer=S.LAYER_PROCEDURE, **kw):
    return {"layer": layer, "handle": handle, "text": text, **kw}


# ── Only memory counts as a turn-level evolution ─────────────────────────────


def test_a_written_procedure_is_reported_with_its_handle():
    summary = S.settle_turn(
        message_id="m1",
        memory_entries=[_entry(why="同名公司会串数据", applies_to="财务分析")],
    )
    assert summary.state == S.SETTLE_SETTLED
    assert summary.status == S.ST_WRITTEN
    assert summary.gain == 1
    entry = summary.entries[0]
    assert entry.handle == "m-1"
    assert entry.why == "同名公司会串数据"
    assert entry.layer == S.LAYER_PROCEDURE


def test_a_profile_field_is_reported_with_its_key_as_the_handle():
    summary = S.settle_turn(
        message_id="m1",
        memory_entries=[_entry(handle="identity.dept", text="研发中心", layer=S.LAYER_PROFILE)],
    )
    assert summary.entries[0].handle == "identity.dept"
    assert summary.entries[0].layer == S.LAYER_PROFILE


def test_an_entry_without_a_handle_is_dropped():
    # The card offers edit and delete on every row. An entry it cannot address
    # would render two buttons that fail, so it is not rendered at all.
    summary = S.settle_turn(message_id="m1", memory_entries=[_entry(handle="")])
    assert summary.state == S.SETTLE_EMPTY
    assert summary.entries == []


def test_a_turn_that_wrote_nothing_settles_empty():
    summary = S.settle_turn(message_id="m1", memory_entries=[])
    assert summary.state == S.SETTLE_EMPTY
    assert summary.gain == 0


def test_memory_disabled_reports_nothing_even_if_entries_leak_through():
    summary = S.settle_turn(message_id="m1", memory_enabled=False, memory_entries=[_entry()])
    assert summary.state == S.SETTLE_EMPTY


def test_a_write_failure_is_stated_not_silently_dropped():
    summary = S.settle_turn(message_id="m1", memory_failed=True)
    assert summary.state == S.SETTLE_SETTLED
    assert summary.status == S.ST_FAILED
    assert summary.entries == []


def test_the_settlement_vocabulary_has_no_skill_or_workflow_mechanism():
    # Guards the whole point of this design: a turn cannot report a skill,
    # because using an installed skill is the system working, not evolving, and
    # distilling a new one is a nightly batch that no single turn performs.
    assert set(S._ALLOWED_STATUSES) == {S.MECH_MEMORY}
    assert not hasattr(S, "MECH_SKILL")
    assert not hasattr(S, "MECH_WORKFLOW")


def test_gain_counts_memories_not_mechanisms():
    summary = S.settle_turn(
        message_id="m1",
        memory_entries=[_entry(handle="a"), _entry(handle="b"), _entry(handle="c")],
    )
    assert summary.gain == 3


def test_summary_roundtrips_through_dict():
    original = S.settle_turn(
        message_id="m1",
        episode_id="ep1",
        memory_entries=[_entry(why="口径不同会算错", applies_to="周报")],
    )
    restored = S.EvolutionSummary.from_dict(original.to_dict())
    assert restored.state == original.state
    assert restored.gain == original.gain
    assert restored.entries[0].handle == "m-1"
    assert restored.entries[0].applies_to == "周报"


def test_pending_payload_carries_only_what_the_client_must_watch():
    payload = S.pending_payload(message_id="m1")
    assert payload["message_id"] == "m1"
    assert payload["state"] == S.SETTLE_PENDING
    # No mechanism list: the client renders nothing until settlement lands, so
    # there is nothing to pre-announce.
    assert "expected" not in payload


def test_failed_summary_is_terminal_not_a_spinner():
    summary = S.failed_summary(message_id="m1", error="boom")
    assert summary.state == S.SETTLE_FAILED
    assert summary.error == "boom"
