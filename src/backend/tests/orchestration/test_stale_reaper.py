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
    lease_seconds: float | None = None,
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
                lease_owner="remote-worker" if lease_seconds is not None else None,
                lease_expires_at=(
                    datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
                    if lease_seconds is not None
                    else None
                ),
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


async def test_hard_max_age_preserves_bounded_human_wait(reaper_env, monkeypatch):
    """A question opened near the run hard cap keeps its own full wait window."""

    session_factory, fake_redis = reaper_env
    _insert_run(session_factory, "run_human_wait", age_sec=executor._HARD_MAX_AGE_SEC + 300)
    fake_redis.seed(executor._stream_key("run_human_wait"), _now_ms() - 1_000)
    monkeypatch.setattr(executor.human_interaction, "has_pending", lambda chat_id: bool(chat_id))

    assert await executor.reap_stale_runs() == 0
    assert _get_run(session_factory, "run_human_wait").status == "running"


async def test_young_run_untouched(reaper_env):
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_young", age_sec=60)

    assert await executor.reap_stale_runs() == 0
    assert _get_run(session_factory, "run_young").status == "running"


async def test_valid_cross_process_lease_shields_a_quiet_run(reaper_env):
    """The DB ownership lease outranks a missing local task or Redis activity."""
    session_factory, _ = reaper_env
    _insert_run(
        session_factory,
        "run_remote",
        age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300,
        lease_seconds=60,
    )

    assert await executor.reap_stale_runs() == 0
    run = _get_run(session_factory, "run_remote")
    assert run.status == "running"
    assert run.lease_owner == "remote-worker"


async def test_reaper_cas_cannot_kill_a_worker_claimed_after_candidate_scan(
    reaper_env, monkeypatch
):
    session_factory, _ = reaper_env
    _insert_run(
        session_factory,
        "run_claim_race",
        age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300,
    )

    async def claim_during_activity_probe(_run_id):
        assert executor._journal().claim(
            "run_claim_race",
            owner="new-worker",
            lease_seconds=60,
        )
        return None

    monkeypatch.setattr(executor, "_stream_last_write_ms", claim_during_activity_probe)

    assert await executor.reap_stale_runs() == 0
    run = _get_run(session_factory, "run_claim_race")
    assert run.status == "running"
    assert run.lease_owner == "new-worker"


# ─── Reaping aligned with in-process task cancellation ──────────────────────────────────────────


async def test_quiet_over_age_run_with_live_task_survives(reaper_env, monkeypatch):
    """A live in-process worker task is never a zombie — the inactivity watchdog
    owns hang detection for it. Stream-quiet reaping killed healthy long runs
    whose model was streaming huge tool-call args (which map to no stream
    events); the quiet check now applies only to orphans."""
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_local", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)

    task = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setitem(executor._active_runs, "run_local", task)

    assert await executor.reap_stale_runs() == 0
    assert _get_run(session_factory, "run_local").status == "running"
    assert not task.cancelled()
    task.cancel()


def _insert_job(session_factory, job_id: str, *, chat_id: str, status: str, updated_age_sec: float):
    from core.db.models import Job

    with session_factory() as db:
        db.add(
            Job(
                job_id=job_id,
                user_id="user_test",
                chat_id=chat_id,
                name="t",
                status=status,
                budget={},
                usage={},
                extra_data={},
                updated_at=datetime.now(timezone.utc) - timedelta(seconds=updated_age_sec),
            )
        )
        db.commit()


async def test_quiet_orphan_with_live_job_survives(reaper_env):
    """工作流模式：run 卡在 run_job(wait=True) 里等作业，主链路一个流事件都不产生。

    进程内 task 豁免只在本进程有效——多 worker / 多副本部署里另一个进程看不到这个
    task，会按"流静默"把健康的长作业误杀（实测踩过：双后端共库时主栈把测试后端的
    run 杀了）。作业活性是跨进程可见的证据，必须能救下这个 run。
    """
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_job_wait", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)
    _insert_job(
        session_factory, "job_live", chat_id="chat_test", status="running", updated_age_sec=5
    )

    assert await executor.reap_stale_runs() == 0
    assert _get_run(session_factory, "run_job_wait").status == "running"


async def test_quiet_orphan_with_stalled_job_is_reaped(reaper_env):
    """作业本身也不动了（updated_at 落在静默窗口之外）→ 不是活性证据，照常回收。"""
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_job_dead", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)
    _insert_job(
        session_factory,
        "job_stalled",
        chat_id="chat_test",
        status="running",
        updated_age_sec=executor._STALE_QUIET_SEC + 120,
    )

    assert await executor.reap_stale_runs() == 1
    assert _get_run(session_factory, "run_job_dead").status == "failed"


async def test_terminal_job_does_not_shield_run(reaper_env):
    """作业已终态 → 不再是活性证据，run 该回收就回收（防止豁免变成永生通行证）。"""
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_job_done", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)
    _insert_job(
        session_factory, "job_done", chat_id="chat_test", status="completed", updated_age_sec=5
    )

    assert await executor.reap_stale_runs() == 1
    assert _get_run(session_factory, "run_job_done").status == "failed"


async def test_orphan_with_done_task_is_reaped(reaper_env, monkeypatch):
    """A finished task left in _active_runs does not shield the run: quiet orphans are reaped."""
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_done", age_sec=executor._STALE_RUN_MAX_AGE_SEC + 300)

    task = asyncio.create_task(asyncio.sleep(0))
    await task  # completed task still parked in the registry
    monkeypatch.setitem(executor._active_runs, "run_done", task)

    assert await executor.reap_stale_runs() == 1
    assert _get_run(session_factory, "run_done").status == "failed"


async def test_hard_expired_cancels_local_worker_task(reaper_env, monkeypatch):
    """Past the absolute lifetime cap, even a live in-process task is reaped and cancelled."""
    session_factory, _ = reaper_env
    _insert_run(session_factory, "run_local_hard", age_sec=executor._HARD_MAX_AGE_SEC + 300)

    task = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setitem(executor._active_runs, "run_local_hard", task)

    assert await executor.reap_stale_runs() == 1
    await asyncio.sleep(0)  # let the cancel propagate
    assert task.cancelled()


async def test_hard_expired_leaves_plan_execute_task_to_cooperative_stop(reaper_env, monkeypatch):
    """plan_execute does no cross-task cancel (anyio deadlock risk); it self-stops via polling."""
    session_factory, _ = reaper_env
    _insert_run(
        session_factory,
        "run_plan",
        age_sec=executor._HARD_MAX_AGE_SEC + 300,
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


# ─── 自主循环豁免年龄硬顶（去预算化配套） ──────────────────────────────────────


async def test_hard_expired_loop_with_live_task_survives(reaper_env, monkeypatch):
    """自主循环的使命是跑到任务完成为止：进程内 task 存活的 loop run 即使超过
    CHAT_RUN_HARD_MAX_AGE_SEC 也不得按年龄硬杀（历史竞态：6h 硬顶 == loop 旧默认
    预算，跑满预算的健康 loop 在优雅收尾前被硬顶抢杀）。"""
    session_factory, _ = reaper_env
    _insert_run(
        session_factory,
        "run_loop_live",
        age_sec=executor._HARD_MAX_AGE_SEC + 3600,
        kind="autonomous_loop",
    )

    task = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setitem(executor._active_runs, "run_loop_live", task)

    assert await executor.reap_stale_runs() == 0
    assert _get_run(session_factory, "run_loop_live").status == "running"
    assert not task.cancelled()
    task.cancel()


async def test_hard_expired_orphan_loop_reaped_by_quiet_rule_only(reaper_env):
    """孤儿 loop（进程重启遗留、无活跃 task）不按年龄硬杀，但静默判据照常清理。"""
    session_factory, fake_redis = reaper_env
    _insert_run(
        session_factory,
        "run_loop_orphan",
        age_sec=executor._HARD_MAX_AGE_SEC + 3600,
        kind="autonomous_loop",
    )
    # 流最近还在写 → 不是僵尸，跳过
    fake_redis.seed(executor._stream_key("run_loop_orphan"), _now_ms() - 5_000)
    assert await executor.reap_stale_runs() == 0
    assert _get_run(session_factory, "run_loop_orphan").status == "running"

    # 流静默超阈值 → 按「无活动」清理（而非年龄硬顶），错误文案是 stalled
    fake_redis.seed(
        executor._stream_key("run_loop_orphan"),
        _now_ms() - int(executor._STALE_QUIET_SEC * 1000) - 60_000,
    )
    assert await executor.reap_stale_runs() == 1
    run = _get_run(session_factory, "run_loop_orphan")
    assert run.status == "failed"
    assert "stalled" in run.error_message


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
