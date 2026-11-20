"""Profile compaction is scheduled as a durable outbox row, not an event-loop task.

Profile writes run in a DB worker thread. Scheduling compaction must commit a
``profile_compact`` MemoryOutbox row from that thread (no running loop needed)
so a process exit cannot lose the work; the startup worker resumes it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.db.models import MemoryOutbox
from core.memory import profile as profile_mod
from core.memory.context import MemoryContext
from core.memory.outbox import common as outbox_common
from tests.memory.outbox_test_support import _configure_db, _memory_settings


@pytest.fixture
def factory(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    monkeypatch.setattr(profile_mod, "_audit_sync_safe", lambda *_args: None)
    return factory


def _compact_jobs(factory, user_id):
    with factory() as db:
        return [
            row
            for row in db.query(MemoryOutbox).filter(MemoryOutbox.job_kind == "profile_compact").all()
            if (row.payload_json or {}).get("ctx", {}).get("user_id") == user_id
        ]


def test_schedule_compact_commits_outbox_row_without_event_loop(factory):
    ctx = MemoryContext(user_id="u-sched", message_id="m-sched", write_enabled=True)

    profile_mod._schedule_compact(ctx, None)

    jobs = _compact_jobs(factory, "u-sched")
    assert len(jobs) == 1
    assert jobs[0].layer == "L1:compact"


def test_schedule_compact_swallows_enqueue_failure(factory, monkeypatch):
    def boom(_ctx):
        raise RuntimeError("db down")

    monkeypatch.setattr("core.memory.outbox.enqueue_profile_compaction", boom)

    profile_mod._schedule_compact(MemoryContext(user_id="u-fail", write_enabled=True), None)

    assert _compact_jobs(factory, "u-fail") == []


@pytest.mark.parametrize("max_chars, expected_jobs", [(5, 1), (10_000, 0)])
def test_patch_enqueues_compaction_only_when_over_limit(factory, monkeypatch, max_chars, expected_jobs):
    monkeypatch.setattr(
        profile_mod,
        "settings",
        SimpleNamespace(memory=_memory_settings(profile_max_chars=max_chars)),
    )
    user_id = f"u-patch-{max_chars}"
    ctx = MemoryContext(user_id=user_id, message_id=f"m-{max_chars}", write_enabled=True)

    assert profile_mod._patch_sync(ctx, "喜欢简洁的回答", None, []) is True

    assert len(_compact_jobs(factory, user_id)) == expected_jobs


def test_upsert_over_limit_enqueues_compaction(factory, monkeypatch):
    monkeypatch.setattr(
        profile_mod, "settings", SimpleNamespace(memory=_memory_settings(profile_max_chars=5))
    )
    ctx = MemoryContext(user_id="u-upsert", message_id="m-upsert", write_enabled=True)

    applied = profile_mod._upsert_fields_sync(
        ctx, [{"key": "identity.name", "value": "小明", "hits": [], "reason": None}]
    )

    assert applied == [{"key": "identity.name", "value": "小明", "action": "write"}]
    assert len(_compact_jobs(factory, "u-upsert")) == 1
