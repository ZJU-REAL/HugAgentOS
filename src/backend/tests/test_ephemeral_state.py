"""Both ephemeral-state backends must behave identically.

Every case runs twice — once against Redis, once against the in-process map —
because callers (mutexes, daily budgets, one-shot handoffs) pick their backend
from deployment config and must not care which one they got.

The Redis side is exercised through ``fakeredis`` as a *test double*, which is
a different thing from shipping one as a runtime substitute: here a mismatch
fails a test, in production it stalled a chat turn.
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from core.infra.ephemeral import LocalEphemeralState, RedisEphemeralState


@pytest.fixture(params=["local", "redis"])
def state(request, monkeypatch):
    if request.param == "local":
        return LocalEphemeralState()
    client = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    monkeypatch.setattr("core.infra.ephemeral.get_redis", lambda **_kwargs: client)
    return RedisEphemeralState()


@pytest.mark.asyncio
async def test_put_get_roundtrip(state):
    assert await state.get("absent") is None
    await state.put("k", "value", ttl=60)
    assert await state.get("k") == "value"


@pytest.mark.asyncio
async def test_take_reads_once(state):
    """One-shot handoffs (login tickets, steer notes) must not be redeemable twice."""
    await state.put("ticket", "payload", ttl=60)
    assert await state.take("ticket") == "payload"
    assert await state.take("ticket") is None


@pytest.mark.asyncio
async def test_drop_removes_and_tolerates_missing(state):
    await state.put("a", "1", ttl=60)
    await state.drop("a", "never-existed")
    assert await state.get("a") is None


@pytest.mark.asyncio
async def test_bump_counts_up_and_down(state):
    assert await state.bump("count", ttl=60) == 1.0
    assert await state.bump("count", ttl=60) == 2.0
    assert await state.bump("count", -1, ttl=60) == 1.0


@pytest.mark.asyncio
async def test_bump_accumulates_fractions(state):
    """The daily cost budget adds USD amounts, not whole units."""
    await state.bump("cost", 0.25, ttl=60)
    assert await state.bump("cost", 0.5, ttl=60) == pytest.approx(0.75)
    assert float(await state.get("cost")) == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_claim_is_exclusive_until_dropped(state):
    assert await state.claim("lock", ttl=60) is True
    assert await state.claim("lock", ttl=60) is False
    await state.drop("lock")
    assert await state.claim("lock", ttl=60) is True


@pytest.mark.asyncio
async def test_entries_expire_and_release_their_mutex(state):
    """A lapsed lock must become claimable again, or a crashed holder wedges it."""
    await state.put("short", "x", ttl=1)
    assert await state.claim("held", ttl=1) is True
    await asyncio.sleep(1.1)
    assert await state.get("short") is None
    assert await state.claim("held", ttl=60) is True


@pytest.mark.asyncio
async def test_non_positive_ttl_is_rejected_the_same_way(state):
    """Redis refuses these outright; the in-process map must not quietly accept them."""
    for write in (
        lambda: state.put("k", "v", ttl=0),
        lambda: state.bump("k", ttl=-1),
        lambda: state.claim("k", ttl=0),
    ):
        with pytest.raises(ValueError, match="positive"):
            await write()
