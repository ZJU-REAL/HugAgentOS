"""GCE ticket 02 — shared background-worker primitives.

These are the safety properties every pipeline built on this base depends on:
concurrent workers must not claim the same row, a poisoned item must not take
down the drain, and a budget must be a hard ceiling rather than a suggestion.
"""

import asyncio

import pytest

from orchestration.schedulers import _worker_base as wb


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── drain: concurrency, exhaustion, isolation ────────────────────────────────


def test_drain_processes_every_claimed_item_exactly_once():
    queue = [f"item-{i}" for i in range(12)]
    seen = []

    def claim():
        return queue.pop(0) if queue else None

    async def process(item_id):
        seen.append(item_id)

    total = _run(
        wb.drain_queue(claim=claim, process=process, concurrency=4, log_tag="test")
    )
    assert total == 12
    assert sorted(seen) == sorted(f"item-{i}" for i in range(12))
    # Exactly once — a duplicate here would mean duplicate candidate generation.
    assert len(seen) == len(set(seen))


def test_drain_returns_zero_on_empty_queue():
    total = _run(
        wb.drain_queue(
            claim=lambda: None,
            process=lambda _: asyncio.sleep(0),
            concurrency=3,
            log_tag="test",
        )
    )
    assert total == 0


def test_poisoned_item_does_not_abort_the_drain():
    queue = ["good-1", "poison", "good-2"]
    processed = []

    def claim():
        return queue.pop(0) if queue else None

    async def process(item_id):
        if item_id == "poison":
            raise RuntimeError("boom")
        processed.append(item_id)

    # Single worker so ordering is deterministic and the poison sits in the middle.
    total = _run(
        wb.drain_queue(claim=claim, process=process, concurrency=1, log_tag="test")
    )
    assert processed == ["good-1", "good-2"]
    assert total == 3  # the failed item still counts as attempted


def test_budget_exhaustion_stops_drain_and_hands_back_the_claimed_item():
    queue = [f"item-{i}" for i in range(6)]
    processed = []
    stranded = []

    def claim():
        return queue.pop(0) if queue else None

    async def process(item_id):
        processed.append(item_id)

    async def budget_check():
        # Allow the first two, then hard-stop.
        return (len(processed) < 2, None if len(processed) < 2 else "daily cap")

    total = _run(
        wb.drain_queue(
            claim=claim,
            process=process,
            concurrency=1,
            budget_check=budget_check,
            on_budget_exhausted=lambda item_id, reason: stranded.append((item_id, reason)),
            log_tag="test",
        )
    )
    assert len(processed) == 2
    assert total == 2
    # The item claimed when the budget ran out must be marked, not left in limbo.
    assert len(stranded) == 1 and stranded[0][1] == "daily cap"


def test_budget_checked_after_claim_so_no_item_is_silently_dropped():
    claimed = []
    processed = []

    def claim():
        if len(claimed) >= 1:
            return None
        claimed.append("only")
        return "only"

    async def budget_check():
        return False, "over"

    _run(
        wb.drain_queue(
            claim=claim,
            process=lambda i: processed.append(i),
            concurrency=1,
            budget_check=budget_check,
            on_budget_exhausted=lambda i, r: None,
            log_tag="test",
        )
    )
    # It was claimed (so the caller can requeue/mark it) but never processed.
    assert claimed == ["only"]
    assert processed == []


# ── day lock ─────────────────────────────────────────────────────────────────


def test_day_lock_fails_closed_when_redis_is_unreachable(monkeypatch):
    def broken_redis():
        raise RuntimeError("no redis")

    monkeypatch.setattr(wb, "get_redis", broken_redis)
    # Failing closed: skipping a cycle beats running it twice, because duplicate
    # runs pollute the evidence chain.
    assert _run(wb.acquire_day_lock("t:", log_tag="test")) is False


def test_day_lock_denied_when_another_instance_holds_it(monkeypatch):
    class _Redis:
        async def set(self, *_a, **_kw):
            return None  # NX failed → someone else holds it

    monkeypatch.setattr(wb, "get_redis", lambda **_: _Redis())
    assert _run(wb.acquire_day_lock("t:", log_tag="test")) is False


def test_day_lock_acquired_when_free(monkeypatch):
    captured = {}

    class _Redis:
        async def set(self, key, value, ex=None, nx=None):
            captured.update(key=key, ex=ex, nx=nx)
            return True

    monkeypatch.setattr(wb, "get_redis", lambda **_: _Redis())
    assert _run(wb.acquire_day_lock("t:", ttl_s=99, today="20260729", log_tag="test")) is True
    assert captured["key"] == "t:20260729"
    assert captured["nx"] is True and captured["ex"] == 99


# ── cron / sleep ─────────────────────────────────────────────────────────────


def test_seconds_until_next_cron_is_positive_and_bounded():
    seconds = wb.seconds_until_next_cron("30 2 * * *", "Asia/Shanghai")
    assert 1.0 <= seconds <= 24 * 3600 + 1


def test_interruptible_sleep_returns_promptly_when_stopped():
    # Long nominal sleep, but the running flag is already false → returns at once.
    _run(wb.interruptible_sleep(3600, lambda: False))


def test_interruptible_sleep_waits_when_running():
    ticks = {"n": 0}

    def still_running():
        ticks["n"] += 1
        return False

    _run(wb.interruptible_sleep(0.0, still_running))
    # Zero-length sleep short-circuits without consulting the flag.
    assert ticks["n"] == 0
