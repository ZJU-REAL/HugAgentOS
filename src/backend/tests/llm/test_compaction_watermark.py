"""Regression tests for sequence-watermarked, CAS-safe compaction."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

import core.db.engine as engine_mod
from core.db.models import ChatMessage, ChatSession
from core.db.repository.chat import ChatMessageRepository
from core.llm import compaction as C
from core.services import compaction_service as S
from core.services.chat_service import ChatService, CompactionCASConflict
from sqlalchemy.orm import sessionmaker

CHAT_ID = "chat_compaction_watermark"


def _session(db) -> ChatService:
    db.add(ChatSession(chat_id=CHAT_ID, user_id="u1", title="watermark"))
    db.commit()
    return ChatService(db)


def _message(svc: ChatService, message_id: str, role: str, content: str) -> ChatMessage:
    return svc.message_repo.create(
        {
            "message_id": message_id,
            "chat_id": CHAT_ID,
            "role": role,
            "content": content,
        }
    )


def test_direct_orm_message_writes_receive_non_null_sequences(db_session):
    _session(db_session)
    first = ChatMessage(
        message_id="direct-1", chat_id=CHAT_ID, role="user", content="one"
    )
    second = ChatMessage(
        message_id="direct-2", chat_id=CHAT_ID, role="assistant", content="two"
    )
    db_session.add_all([second, first])
    db_session.commit()

    rows = (
        db_session.query(ChatMessage)
        .filter(ChatMessage.chat_id == CHAT_ID)
        .order_by(ChatMessage.chat_seq)
        .all()
    )
    assert [(row.message_id, row.chat_seq) for row in rows] == [
        ("direct-2", 1),
        ("direct-1", 2),
    ]
    assert db_session.get(ChatSession, CHAT_ID).next_message_seq == 3


def test_sqlite_two_session_sequence_allocation_serializes_without_collision(
    db_session,
):
    _session(db_session)
    make_session = sessionmaker(bind=db_session.get_bind())
    first_db = make_session()
    started = threading.Event()
    errors: list[BaseException] = []

    try:
        ChatMessageRepository(first_db).create(
            {
                "message_id": "concurrent-1",
                "chat_id": CHAT_ID,
                "role": "user",
                "content": "first",
            },
            commit=False,
        )

        def write_second() -> None:
            try:
                with make_session() as second_db:
                    started.set()
                    ChatMessageRepository(second_db).create(
                        {
                            "message_id": "concurrent-2",
                            "chat_id": CHAT_ID,
                            "role": "assistant",
                            "content": "second",
                        }
                    )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        worker = threading.Thread(target=write_second)
        worker.start()
        assert started.wait(timeout=1)
        # Let the second connection reach SQLite's write lock before releasing
        # the first reservation. It must then re-evaluate the atomic UPDATE.
        time.sleep(0.1)
        first_db.commit()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert errors == []
    finally:
        first_db.close()

    db_session.expire_all()
    rows = (
        db_session.query(ChatMessage)
        .filter(ChatMessage.chat_id == CHAT_ID)
        .order_by(ChatMessage.chat_seq)
        .all()
    )
    assert [(row.message_id, row.chat_seq) for row in rows] == [
        ("concurrent-1", 1),
        ("concurrent-2", 2),
    ]


def test_replay_uses_chat_seq_when_created_at_is_identical(db_session):
    svc = _session(db_session)
    first = _message(svc, "m1", "user", "before")
    second = _message(svc, "m2", "assistant", "answer")
    checkpoint = svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("first summary"),
        replacement_history=[
            {"role": "user", "content": C.format_summary_text("first summary")}
        ],
        covered_seq=second.chat_seq,
    )
    tail = _message(svc, "m3", "user", "same-time tail")

    # Timestamps are deliberately ambiguous. Sequence numbers are the only
    # allowed replay boundary.
    tail.created_at = checkpoint.created_at = first.created_at
    db_session.commit()

    history = S._load_history(svc, CHAT_ID)
    rendered = "\n".join(
        S._render_content_for_summary(item["content"]) for item in history
    )
    assert "first summary" in rendered
    assert "same-time tail" in rendered
    assert "answer" not in rendered


def test_legacy_checkpoint_without_watermark_is_invalidated_and_rebuilt(db_session):
    svc = _session(db_session)
    _message(svc, "m1", "user", "legacy source question")
    _message(svc, "m2", "assistant", "legacy source answer")
    # In the old protocol this row could arrive while summarization was in
    # flight, before the checkpoint itself was finally inserted.
    _message(svc, "m3", "user", "arrived during legacy summary")
    svc.message_repo.create(
        {
            "message_id": "legacy-checkpoint",
            "chat_id": CHAT_ID,
            "role": "system",
            "content": C.format_summary_text("unsafe legacy summary"),
            "extra_data": {
                "kind": C.COMPACTION_CHECKPOINT_KIND,
                "replacement_history": [
                    {
                        "role": "user",
                        "content": C.format_summary_text("unsafe legacy summary"),
                    }
                ],
                # Deliberately no covered_seq/source_hash.
                "active": True,
            },
        }
    )

    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None
    snapshot = svc.acquire_compaction_snapshot(
        CHAT_ID, owner="migrator", lease_seconds=60
    )
    assert snapshot is not None
    assert snapshot.base_checkpoint_id is None
    assert snapshot.base_checkpoint_version == 0
    assert snapshot.base_covered_seq == 0
    assert snapshot.source_message_ids == ("m1", "m2", "m3")


def test_message_arriving_during_summary_stays_in_tail(db_session, monkeypatch):
    svc = _session(db_session)
    _message(svc, "m1", "user", "snapshot question")
    _message(svc, "m2", "assistant", "snapshot answer")
    monkeypatch.setattr(
        engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind())
    )

    async def summarize_then_insert(history, *, timeout):
        with engine_mod.SessionLocal() as other:
            _message(ChatService(other), "m3", "user", "arrived while summarizing")
        return "snapshot summary"

    monkeypatch.setattr(S, "_summarize", summarize_then_insert)

    assert asyncio.run(S.run_post_turn_compaction(CHAT_ID)) is True
    checkpoint = svc.get_latest_compaction_checkpoint(CHAT_ID)
    assert checkpoint is not None
    assert checkpoint.extra_data["covered_seq"] == 2
    history = S._load_history(svc, CHAT_ID)
    assert any(
        "arrived while summarizing" in str(item.get("content")) for item in history
    )


def test_same_base_checkpoint_has_exactly_one_cas_winner(db_session):
    svc = _session(db_session)
    source = _message(svc, "m1", "user", "source")
    owner = "worker-a"
    snapshot = svc.acquire_compaction_snapshot(CHAT_ID, owner=owner, lease_seconds=60)
    assert snapshot is not None and snapshot.covered_seq == source.chat_seq

    svc.commit_compaction_checkpoint(
        snapshot,
        owner=owner,
        summary_text=C.format_summary_text("winner"),
        replacement_history=[
            {"role": "user", "content": C.format_summary_text("winner")}
        ],
        replacement_manifest={"source_count": 1},
    )
    try:
        svc.commit_compaction_checkpoint(
            snapshot,
            owner=owner,
            summary_text=C.format_summary_text("loser"),
            replacement_history=[
                {"role": "user", "content": C.format_summary_text("loser")}
            ],
            replacement_manifest={"source_count": 1},
        )
    except CompactionCASConflict:
        pass
    else:
        raise AssertionError("the second successor from the same base must lose CAS")

    checkpoints = [
        row
        for row in db_session.query(ChatMessage)
        .filter(ChatMessage.chat_id == CHAT_ID)
        .all()
        if (row.extra_data or {}).get("kind") == C.COMPACTION_CHECKPOINT_KIND
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0].extra_data["checkpoint_version"] == 1


def test_two_compactors_overlap_but_only_lease_holder_publishes(
    db_session, monkeypatch
):
    svc = _session(db_session)
    _message(svc, "m1", "user", "source")
    monkeypatch.setattr(
        engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind())
    )
    summarizing = asyncio.Event()
    finish = asyncio.Event()

    async def held_summary(history, *, timeout):
        summarizing.set()
        await finish.wait()
        return "winner"

    monkeypatch.setattr(S, "_summarize", held_summary)

    async def overlap() -> tuple[bool, bool]:
        first = asyncio.create_task(S.run_post_turn_compaction(CHAT_ID))
        await asyncio.wait_for(summarizing.wait(), timeout=2)
        second = await S.run_post_turn_compaction(CHAT_ID)
        finish.set()
        return await first, second

    assert asyncio.run(overlap()) == (True, False)
    checkpoints = [
        row
        for row in db_session.query(ChatMessage)
        .filter(ChatMessage.chat_id == CHAT_ID)
        .all()
        if (row.extra_data or {}).get("kind") == C.COMPACTION_CHECKPOINT_KIND
    ]
    assert len(checkpoints) == 1


def test_history_rewrite_invalidates_lineage_and_fences_inflight_summary(db_session):
    svc = _session(db_session)
    _message(svc, "m1", "user", "retained question")
    first_answer = _message(svc, "m2", "assistant", "retained answer")
    svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("first summary"),
        replacement_history=[
            {"role": "user", "content": C.format_summary_text("first summary")}
        ],
        covered_seq=first_answer.chat_seq,
    )
    _message(svc, "m3", "user", "rewrite from here")
    _message(svc, "m4", "assistant", "soon deleted")
    stale = svc.acquire_compaction_snapshot(CHAT_ID, owner="stale", lease_seconds=60)
    assert stale is not None

    assert svc.delete_messages_from(CHAT_ID, "m3") >= 2
    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None
    try:
        svc.commit_compaction_checkpoint(
            stale,
            owner="stale",
            summary_text=C.format_summary_text("must not commit"),
            replacement_history=[
                {"role": "user", "content": C.format_summary_text("must not commit")}
            ],
            replacement_manifest={},
        )
    except CompactionCASConflict:
        pass
    else:
        raise AssertionError("a history rewrite must fence the in-flight summary")

    rebuilt = svc.acquire_compaction_snapshot(CHAT_ID, owner="fresh", lease_seconds=60)
    assert rebuilt is not None
    assert rebuilt.base_checkpoint_id is None
    assert rebuilt.base_covered_seq == 0
    assert rebuilt.source_message_ids == ("m1", "m2")


def test_accepting_covered_revision_invalidates_checkpoint_and_inflight_snapshot(
    db_session,
):
    svc = _session(db_session)
    _message(svc, "m1", "user", "question")
    answer = svc.message_repo.create(
        {
            "message_id": "m2",
            "chat_id": CHAT_ID,
            "role": "assistant",
            "content": "old answer",
            "extra_data": {
                "ontology_governance": {
                    "review": {"candidate_answer": "accepted revised answer"}
                }
            },
        }
    )
    svc.add_compaction_checkpoint(
        CHAT_ID,
        summary_text=C.format_summary_text("old summary"),
        replacement_history=[
            {"role": "user", "content": C.format_summary_text("old summary")}
        ],
        covered_seq=answer.chat_seq,
    )
    _message(svc, "m3", "user", "tail")
    stale = svc.acquire_compaction_snapshot(CHAT_ID, owner="stale", lease_seconds=60)
    assert stale is not None

    updated = svc.accept_ontology_revision("m2")
    assert updated is not None and updated.content == "accepted revised answer"
    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None
    try:
        svc.commit_compaction_checkpoint(
            stale,
            owner="stale",
            summary_text=C.format_summary_text("stale summary"),
            replacement_history=[],
            replacement_manifest={},
        )
    except CompactionCASConflict:
        pass
    else:
        raise AssertionError("accepted revision must fence the in-flight summary")

    rendered = "\n".join(
        S._render_content_for_summary(item["content"])
        for item in S._load_history(svc, CHAT_ID)
    )
    assert "accepted revised answer" in rendered
    assert "old summary" not in rendered


def test_checkpoint_write_failure_is_explicit_and_emits_no_fake_notice(
    db_session, monkeypatch
):
    svc = _session(db_session)
    _message(svc, "m1", "user", "source")
    monkeypatch.setattr(
        engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind())
    )

    async def fake_summary(history, *, timeout):
        return "summary"

    def fail_write(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(S, "_summarize", fake_summary)
    monkeypatch.setattr(ChatService, "commit_compaction_checkpoint", fail_write)

    assert asyncio.run(S.run_post_turn_compaction(CHAT_ID)) is False
    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None
    assert S.pop_compaction_notice(svc, CHAT_ID) is False


def test_context_budget_estimator_has_stable_component_manifest():
    estimate = S.estimate_context_budget(
        system_prompt="system",
        tool_schema=[{"type": "function", "function": {"name": "lookup"}}],
        messages=[
            {"role": "user", "content": "question"},
            {
                "role": "tool",
                "content": [
                    {
                        "type": "tool_result",
                        "name": "lookup",
                        "output": "x" * 100_000,
                    }
                ],
            },
        ],
        provider_overhead_tokens=17,
    )
    assert estimate == {
        "system_prompt_tokens": C.approx_token_count("system"),
        "tool_schema_tokens": C.approx_token_count(
            '[{"function":{"name":"lookup"},"type":"function"}]'
        ),
        "message_tokens": S.estimate_history_tokens(
            [
                {"role": "user", "content": "question"},
                {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool_result",
                            "name": "lookup",
                            "output": "x" * 100_000,
                        }
                    ],
                },
            ]
        ),
        "provider_overhead_tokens": 17,
        "total_estimated_tokens": 0,
    } | {
        "total_estimated_tokens": sum(
            value for key, value in estimate.items() if key != "total_estimated_tokens"
        )
    }


def test_pre_turn_rechecks_authoritative_snapshot_before_summarizing(
    db_session, monkeypatch
):
    svc = _session(db_session)
    _message(svc, "m1", "user", "small authoritative history")
    monkeypatch.setattr(
        engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind())
    )
    real = S.settings
    cfg = dataclasses.replace(real.compaction, token_limit=100)
    monkeypatch.setattr(S, "settings", dataclasses.replace(real, compaction=cfg))

    async def must_not_summarize(history, *, timeout):
        raise AssertionError("authoritative snapshot is below the threshold")

    monkeypatch.setattr(S, "_summarize", must_not_summarize)
    stale_caller_history = [
        {
            "role": "tool",
            "content": [
                {"type": "tool_result", "name": "huge", "output": "x" * 100_000}
            ],
        }
    ]
    returned, compacted = asyncio.run(
        S.maybe_run_pre_turn_compaction(
            CHAT_ID,
            stale_caller_history,
            model_name="test-model",
            context_window=10_000,
        )
    )
    assert compacted is False
    assert returned == stale_caller_history
    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None


def test_pre_turn_and_post_turn_store_the_same_budget_manifest_shape(
    db_session, monkeypatch
):
    svc = _session(db_session)
    _message(svc, "m1", "user", "question" * 100)
    monkeypatch.setattr(
        engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind())
    )
    real = S.settings
    tiny = dataclasses.replace(real.compaction, token_limit=1)
    monkeypatch.setattr(S, "settings", dataclasses.replace(real, compaction=tiny))

    async def fake_summary(history, *, timeout):
        return "summary"

    monkeypatch.setattr(S, "_summarize", fake_summary)
    history = S._load_history(svc, CHAT_ID)
    _, compacted = asyncio.run(
        S.maybe_run_pre_turn_compaction(
            CHAT_ID,
            history,
            model_name="test-model",
            context_window=10_000,
            system_prompt="sys",
            tool_schema=[{"name": "tool"}],
            provider_overhead_tokens=9,
        )
    )
    assert compacted is True
    first = svc.get_latest_compaction_checkpoint(CHAT_ID)
    first_budget = first.extra_data["replacement_manifest"]["budget_estimate"]
    assert set(first_budget) == {
        "system_prompt_tokens",
        "tool_schema_tokens",
        "message_tokens",
        "provider_overhead_tokens",
        "total_estimated_tokens",
    }

    _message(svc, "m2", "assistant", "post-turn answer")
    post_inputs = {
        "system_prompt": "post-system",
        "tool_schema": [{"name": "post-tool"}],
        "provider_overhead_tokens": 13,
    }
    assert (
        asyncio.run(S.run_post_turn_compaction(CHAT_ID, budget_inputs=post_inputs))
        is True
    )
    second = svc.get_latest_compaction_checkpoint(CHAT_ID)
    second_budget = second.extra_data["replacement_manifest"]["budget_estimate"]
    assert set(second_budget) == set(first_budget)
    assert second_budget["system_prompt_tokens"] == C.approx_token_count("post-system")
    assert second_budget["tool_schema_tokens"] == C.approx_token_count(
        '[{"name":"post-tool"}]'
    )
    assert second_budget["provider_overhead_tokens"] == 13


def test_rolling_compaction_keeps_one_lineage_without_duplicate_or_loss(
    db_session, monkeypatch
):
    svc = _session(db_session)
    _message(svc, "m1", "user", "round-one-question")
    _message(svc, "m2", "assistant", "round-one-answer")
    monkeypatch.setattr(
        engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind())
    )
    summaries = iter(("round-one-summary", "round-two-summary"))

    async def fake_summary(history, *, timeout):
        return next(summaries)

    monkeypatch.setattr(S, "_summarize", fake_summary)
    assert asyncio.run(S.run_post_turn_compaction(CHAT_ID)) is True
    _message(svc, "m3", "user", "round-two-question")
    _message(svc, "m4", "assistant", "round-two-answer")
    assert asyncio.run(S.run_post_turn_compaction(CHAT_ID)) is True

    history = S._load_history(svc, CHAT_ID)
    rendered = "\n".join(
        S._render_content_for_summary(item["content"]) for item in history
    )
    assert rendered.count("round-two-question") == 1
    assert "round-two-summary" in rendered
    assert "round-one-summary" not in rendered
    assert "round-one-answer" not in rendered
    assert "round-two-answer" not in rendered

    checkpoints = [
        row
        for row in db_session.query(ChatMessage)
        .filter(ChatMessage.chat_id == CHAT_ID)
        .all()
        if (row.extra_data or {}).get("kind") == C.COMPACTION_CHECKPOINT_KIND
    ]
    assert len(checkpoints) == 2
    assert sum(bool((row.extra_data or {}).get("active")) for row in checkpoints) == 1
    latest = svc.get_latest_compaction_checkpoint(CHAT_ID)
    assert latest.extra_data["checkpoint_version"] == 2
    assert latest.extra_data["base_checkpoint_version"] == 1
    assert latest.extra_data["covered_seq"] == 5
