"""Writers report the memories they wrote, item by item.

The card shows each memory and offers to edit or delete it, which is only
possible for an entry it can address. So a writer returns handles — a mem0 id
for an L2 procedure, a profile field key for an L1 entry — and a write whose id
cannot be read back counts as not written, rather than appearing on the card as
a row whose buttons fail.
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


@pytest.fixture(autouse=True)
def _no_near_duplicate(monkeypatch):
    """Pin the dedup gate open: these tests are about write reporting, and must
    not depend on whatever a live Milvus happens to contain."""

    async def _none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(W, "find_similar_procedure", _none)


@pytest.mark.asyncio
async def test_a_written_procedure_comes_back_with_its_id_and_reason(ctx, monkeypatch):
    async def fake_save(**kwargs):
        assert kwargs["memory_meta"]["why"] == "同名公司会串数据"
        return "mem-42"

    monkeypatch.setattr(W, "save_procedure_entry", fake_save)

    written = await W.write_layered(
        {
            ExtractorType.PROCEDURAL: {
                "procedures": [
                    {
                        "rule": "做财务分析前先核验公司主体",
                        "why": "同名公司会串数据",
                        "applies_to": "财务分析",
                    }
                ]
            }
        },
        ctx,
    )

    assert len(written) == 1
    entry = written[0]
    assert entry["layer"] == W.LAYER_PROCEDURE
    assert entry["handle"] == "mem-42"
    assert entry["applies_to"] == "财务分析"


@pytest.mark.asyncio
async def test_a_write_without_a_recoverable_id_is_not_reported(ctx, monkeypatch):
    async def fake_save(**_):
        return None

    monkeypatch.setattr(W, "save_procedure_entry", fake_save)

    written = await W.write_layered(
        {ExtractorType.PROCEDURAL: {"procedures": [{"rule": "先核验主体"}]}}, ctx
    )
    assert written == []


@pytest.mark.asyncio
async def test_the_breaker_being_open_reports_nothing_rather_than_guessing(ctx, monkeypatch):
    monkeypatch.setattr(W.milvus_breaker, "is_open", lambda: True)

    written = await W.write_layered(
        {ExtractorType.PROCEDURAL: {"procedures": [{"rule": "先核验主体"}]}}, ctx
    )
    assert written == []


@pytest.mark.asyncio
async def test_a_profile_field_is_reported_under_its_key(ctx, monkeypatch):
    async def fake_upsert(_ctx, fields):
        return [{"key": k, "value": v, "action": "write"} for k, v, _ in fields]

    monkeypatch.setattr("core.memory.profile.upsert_fields", fake_upsert)

    written = await W.write_layered(
        {ExtractorType.IDENTITY: {"facts": [{"field": "dept", "value": "研发中心"}]}}, ctx
    )

    assert written[0]["layer"] == W.LAYER_PROFILE
    assert written[0]["handle"] == "identity.dept"
    assert written[0]["text"] == "研发中心"


@pytest.mark.asyncio
async def test_an_unchanged_profile_value_is_not_announced_as_a_new_memory(ctx, monkeypatch):
    # upsert_fields returns only what genuinely changed; re-stating what the
    # profile already says must not produce a card.
    async def fake_upsert(_ctx, _fields):
        return []

    monkeypatch.setattr("core.memory.profile.upsert_fields", fake_upsert)

    written = await W.write_layered(
        {ExtractorType.IDENTITY: {"facts": [{"field": "dept", "value": "研发中心"}]}}, ctx
    )
    assert written == []


@pytest.mark.asyncio
async def test_session_tasks_are_written_but_never_shown_as_long_term_memory(ctx, monkeypatch):
    calls = []

    async def fake_session(data, _ctx):
        calls.append(data)
        return 1

    monkeypatch.setattr(W, "_write_session_task", fake_session)

    written = await W.write_layered(
        {ExtractorType.TASK: {"session_task": {"goal": "写周报"}}}, ctx
    )
    # It was written — but it is scoped to this conversation, not remembered.
    assert calls
    assert written == []
