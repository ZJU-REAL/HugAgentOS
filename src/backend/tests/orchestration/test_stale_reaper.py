"""Regression tests for the stale reaper's liveness awareness + terminal-state CAS.

Corresponds to a production incident: age-only reaping killed a 35-minute long task that was
still running tools (the run was marked failed and the SSE was terminated), while the worker
itself was not cancelled and kept running for another 1.5h, overwriting the terminal state
back to completed.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db.engine import Base
from core.db.models import ChatRun
import orchestration.chat_run_executor as executor


# ─── fakes / fixtures ──────────────────────────────────────────────────


class FakeRedis:
    """Implements only the xrevrange / xadd / expire used by the reaper path."""

    def __init__(self):
        self.streams: dict[str, list[tuple[str, dict]]] = {}

    def seed(self, key: str, last_write_ms: int) -> None:
        self.streams[key] = [(f"{last_write_ms}-0", {"data": "{}"})]

    async def xrevrange(self, key, max="+", min="-", count=1):
        entries = self.streams.get(key, [])
        return list(reversed(entries))[:count]

    async def xadd(self, key, fields, maxlen=None, approximate=None):
        self.streams.setdefault(key, []).append(("9999999999999-0", dict(fields)))

    async def expire(self, key, ttl):
        return True


@pytest.fixture()
def reaper_env(monkeypatch):
    """Isolated in-memory sqlite DB + FakeRedis, all patched into the executor module."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    fake_redis = FakeRedis()
    monkeypatch.setattr(executor, "SessionLocal", session_factory)
    monkeypatch.setattr(executor, "get_redis", lambda: fake_redis)
    yield session_factory, fake_redis
    engine.dispose()


def _insert_run(
    session_factory,
    run_id: str,
    *,
    status: str = "running",
    age_sec: float = 0,
    kind: str = "chat",
) -> None:
    began = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    with session_factory() as db:
        db.add(
            ChatRun(
                run_id=run_id,
                chat_id="chat_test",
                user_id="user_test",
                message_id=f"msg_{run_id}",
                status=status,
                request_payload={"kind": kind},
                started_at=began,
                created_at=began,
            )
        )
        db.commit()


def _get_run(session_factory, run_id: str) -> ChatRun:
    with session_factory() as db:
        return db.query(ChatRun).filter(ChatRun.run_id == run_id).first()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ─── reap_stale_runs: liveness awareness ───────────────────────────────────────


async def test_active_over_age_run_survives(reaper_env):
    """An over-age long task whose stream is still producing must not be reaped (production incident scenario)."""
    session_factory, fake_redis = reaper_env
    _insert_run(session_factory, "run_active", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)
    fake_redis.seed(executor._stream_key("run_active"), _now_ms() - 5_000)  # just wrote 5s ago

    assert await executor.reap_stale_runs() == 0
    assert _get_run(session_factory, "run_active").status == "running"


async def test_quiet_over_age_run_is_reaped(reaper_env):
    """Over-age and stream silent past the threshold → reaped to failed and a termination marker written."""
    session_factory, fake_redis = reaper_env
    _insert_run(session_factory, "run_quiet", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)
    fake_redis.seed(
        executor._stream_key("run_quiet"),
        _now_ms() - int(executor._STALE_QUIET_SEC * 1000) - 60_000,
    )

    assert await executor.reap_stale_runs() == 1
    run = _get_run(session_factory, "run_quiet")
    assert run.status == "failed"
    assert "stalled" in run.error_message
    # termination markers written to the stream (error + __terminal__, two entries)
    assert len(fake_redis.streams[executor._stream_key("run_quiet")]) >= 3


async def test_over_age_run_without_stream_is_reaped(reaper_env):
    """Over-age with no stream (worker never wrote / redis lost it) → treated as a zombie and reaped."""
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_nostream", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)

    assert await executor.reap_stale_runs() == 1
    assert _get_run(session_factory, "run_nostream").status == "failed"


async def test_hard_max_age_reaps_even_active_run(reaper_env):
    """Past the absolute lifetime cap, force-reap even if the stream is still active."""
    session_factory, fake_redis = reaper_env
    _insert_run(session_factory, "run_forever", age_sec=executor._HARD_MAX_AGE_SEC + 300)
    fake_redis.seed(executor._stream_key("run_forever"), _now_ms() - 1_000)

    assert await executor.reap_stale_runs() == 1
    run = _get_run(session_factory, "run_forever")
    assert run.status == "failed"
    assert "hard max age" in run.error_message


async def test_young_run_untouched(reaper_env):
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_young", age_sec=60)

    assert await executor.reap_stale_runs() == 0
    assert _get_run(session_factory, "run_young").status == "running"


# ─── Reaping aligned with in-process task cancellation ──────────────────────────────────────────


async def test_reap_cancels_local_worker_task(reaper_env, monkeypatch):
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_local", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)

    task = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setitem(executor._active_runs, "run_local", task)

    assert await executor.reap_stale_runs() == 1
    await asyncio.sleep(0)  # let the cancel propagate
    assert task.cancelled()


async def test_reap_leaves_plan_execute_task_to_cooperative_stop(reaper_env, monkeypatch):
    """plan_execute does no cross-task cancel (anyio deadlock risk); it self-stops via polling."""
    session_factory, _ = reaper_env
    _insert_run(
        session_factory,
        "run_plan",
        age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300,
        kind="plan_execute",
    )

    task = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setitem(executor._active_runs, "run_plan", task)

    assert await executor.reap_stale_runs() == 1
    assert not task.cancelled()
    # DB already declared dead → a cooperative worker polling is_run_cancelled must get True
    assert executor.is_run_cancelled("run_plan") is True
    task.cancel()


# ─── Terminal-state CAS: a late worker cannot overwrite a run already declared dead ──────────────────────


async def test_late_worker_completion_cannot_overwrite_reaped_run(reaper_env):
    """Second half of the incident: after reaping, a worker's late completed write must be rejected."""
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_late", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)
    assert await executor.reap_stale_runs() == 1

    won = executor._finalize_run(
        "run_late",
        status="completed",
        completed_at=datetime.now(timezone.utc),
    )
    assert won is False
    run = _get_run(session_factory, "run_late")
    assert run.status == "failed"
    assert "stalled" in run.error_message


async def test_finalize_run_wins_on_live_run(reaper_env):
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_live", age_sec=10)

    assert executor._finalize_run("run_live", status="completed") is True
    assert _get_run(session_factory, "run_live").status == "completed"


async def test_is_run_cancelled_true_for_any_terminal_status(reaper_env):
    session_factory, _ = reaper_env
    for status, expected in [
        ("running", False),
        ("cancelled", True),
        ("failed", True),
        ("completed", True),
    ]:
        rid = f"run_st_{status}"
        _insert_run(session_factory, rid, status=status)
        assert executor.is_run_cancelled(rid) is expected
