"""Pre-write dedup, strength-tiered TTL, and TTL expiry computation.

The L2 write path used to be a blind append: every extracted procedure became a
new Milvus row, `strength` was stored but never consulted, and `ttl_days` was
never enforced. These tests pin the replacement behavior:

- a near-duplicate reinforces the existing entry instead of appending;
- a weak rule enters provisionally (short TTL), a strong one persists;
- the sweeper's expiry math honours both native expiration_date and legacy
  ttl_days, and never deletes on unparseable data.
"""

import pytest

from core.memory.context import MemoryContext
from core.memory.extractors import writers as W
from core.memory.extractors.router import ExtractorType


@pytest.fixture
def ctx():
    return MemoryContext(user_id="u1", workspace_id="default", write_enabled=True, message_id="m1")


@pytest.fixture(autouse=True)
def _breaker_closed(monkeypatch):
    monkeypatch.setattr(W.milvus_breaker, "is_open", lambda: False)
    monkeypatch.setattr(W.milvus_breaker, "record_success", lambda: None)
    monkeypatch.setattr(W.milvus_breaker, "record_failure", lambda: None)


def _procedural(rule="做财务分析前先核验公司主体", strength="strong"):
    return {
        ExtractorType.PROCEDURAL: {
            "procedures": [{"rule": rule, "why": "", "applies_to": "", "strength": strength}]
        }
    }


@pytest.mark.asyncio
async def test_a_near_duplicate_reinforces_instead_of_appending(ctx, monkeypatch):
    saves = []
    reinforced = []

    async def fake_similar(_ctx, _content, **_):
        return {"id": "mem-old", "memory": "财务分析前先核验主体", "score": 0.95, "metadata": {}}

    async def fake_reinforce(similar, *, strength):
        reinforced.append((similar["id"], strength))
        return True

    async def fake_save(**kwargs):
        saves.append(kwargs)
        return "mem-new"

    monkeypatch.setattr(W, "find_similar_procedure", fake_similar)
    monkeypatch.setattr(W, "reinforce_procedure_entry", fake_reinforce)
    monkeypatch.setattr(W, "save_procedure_entry", fake_save)

    written = await W.write_layered(_procedural(), ctx)

    assert saves == []  # no new row
    assert reinforced == [("mem-old", "strong")]
    assert len(written) == 1
    assert written[0]["handle"] == "mem-old"
    assert written[0]["action"] == "reinforce"


@pytest.mark.asyncio
async def test_a_failed_reinforcement_reports_nothing(ctx, monkeypatch):
    async def fake_similar(_ctx, _content, **_):
        return {"id": "mem-old", "memory": "x", "score": 0.95, "metadata": {}}

    async def fake_reinforce(_similar, *, strength):
        return False

    monkeypatch.setattr(W, "find_similar_procedure", fake_similar)
    monkeypatch.setattr(W, "reinforce_procedure_entry", fake_reinforce)

    written = await W.write_layered(_procedural(), ctx)
    assert written == []


@pytest.mark.asyncio
async def test_strength_decides_the_ttl_of_a_new_entry(ctx, monkeypatch):
    saves = []

    async def fake_similar(_ctx, _content, **_):
        return None

    async def fake_save(**kwargs):
        saves.append(kwargs)
        return f"mem-{len(saves)}"

    monkeypatch.setattr(W, "find_similar_procedure", fake_similar)
    monkeypatch.setattr(W, "save_procedure_entry", fake_save)

    await W.write_layered(_procedural(strength="strong"), ctx)
    await W.write_layered(_procedural(rule="周报口径按自然周", strength="weak"), ctx)

    assert saves[0]["ttl_days"] == W.settings.memory.procedure_ttl_days
    assert saves[0]["memory_meta"]["strength"] == "strong"
    assert saves[1]["ttl_days"] == W.settings.memory.procedure_weak_ttl_days
    assert saves[1]["memory_meta"]["strength"] == "weak"
    assert saves[1]["memory_meta"]["seen_count"] == 1


@pytest.mark.asyncio
async def test_an_unmarked_strength_is_treated_as_weak(ctx, monkeypatch):
    saves = []

    async def fake_similar(_ctx, _content, **_):
        return None

    async def fake_save(**kwargs):
        saves.append(kwargs)
        return "mem-1"

    monkeypatch.setattr(W, "find_similar_procedure", fake_similar)
    monkeypatch.setattr(W, "save_procedure_entry", fake_save)

    await W.write_layered(
        {ExtractorType.PROCEDURAL: {"procedures": [{"rule": "结论放最前面"}]}}, ctx
    )
    assert saves[0]["ttl_days"] == W.settings.memory.procedure_weak_ttl_days


# ── TTL expiry math ────────────────────────────────────────────────────────

from datetime import date

from core.memory.ttl_sweeper import entry_expiry_date


def test_native_expiration_date_wins():
    assert entry_expiry_date({"expiration_date": "2026-01-31"}) == date(2026, 1, 31)
    # native field wins even when legacy fields disagree
    assert entry_expiry_date(
        {"expiration_date": "2026-01-31", "ttl_days": 1, "created_at": "2020-01-01T00:00:00"}
    ) == date(2026, 1, 31)


def test_legacy_entries_expire_from_timestamp_plus_ttl():
    assert entry_expiry_date(
        {"ttl_days": 10, "created_at": "2026-01-01T12:00:00+00:00"}
    ) == date(2026, 1, 11)
    # updated_at (reinforcement bump) extends life over created_at
    assert entry_expiry_date(
        {"ttl_days": 10, "created_at": "2026-01-01T12:00:00", "updated_at": "2026-03-01T00:00:00Z"}
    ) == date(2026, 3, 11)


def test_unparseable_or_missing_data_never_expires():
    assert entry_expiry_date({}) is None
    assert entry_expiry_date({"ttl_days": "not-a-number", "created_at": "2026-01-01T00:00:00"}) is None
    assert entry_expiry_date({"ttl_days": 10}) is None  # no timestamp
    assert entry_expiry_date({"ttl_days": 0, "created_at": "2026-01-01T00:00:00"}) is None
    assert entry_expiry_date({"expiration_date": "soonish"}) is None
    assert entry_expiry_date(None) is None
