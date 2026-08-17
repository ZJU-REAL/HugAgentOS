"""失联作业的对账 + 回调地址探测 + 时间戳时区 —— 三条都对应线上实测到的故障。

事故还原（HugAgentOS 测试机）：一条批量作业提交后，沙箱里的 runner 第一发回调就
``Name or service not known`` 当场死亡——默认回调基址写死了 ``host.docker.internal``，
而那台机器上沙箱与后端同在一张 docker 网络、宿主别名根本解析不了。后果是三重的：

1. 作业永远停在 ``pending``：驱动挂在 ``run_job(wait=True)`` 的工具调用里，用户一中止
   这轮对话驱动就被取消，``drive()`` 里的全部护栏跟着消失，没有任何东西再来收尸；
2. 台账一条都没有，状态条只剩一个转圈的菊花，看不出"到底在干什么"；
3. 状态条上显示「已运行 8 小时 4 分」——作业其实刚提交 4 分钟，8 小时正是容器
   ``TZ=Asia/Shanghai`` 与 naive UTC 时间戳之间的时差。

所以这里锁三件事：孤儿一定会被收、回调不通一定当场拒绝启动、时间戳一定带时区。
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db.engine import Base
from core.db.models import Job
import orchestration.job_runtime as jr


@pytest.fixture()
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(jr, "SessionLocal", Session)
    monkeypatch.setattr(jr, "_active_jobs", {}, raising=False)

    async def _no_wake(job_row_id):
        return None

    monkeypatch.setattr(jr, "_maybe_wake", _no_wake)
    return Session


def _seed(Session, job_id: str, *, status: str, quiet_min: float, token: str = "tok") -> None:
    stale = datetime.now(timezone.utc) - timedelta(minutes=quiet_min)
    with Session() as db:
        db.add(
            Job(
                job_id=job_id,
                user_id="u1",
                chat_id="c1",
                status=status,
                created_at=stale,
                updated_at=stale,
                extra_data={"token": token},
            )
        )
        db.commit()


def _status(Session, job_id: str) -> str:
    with Session() as db:
        return str(db.query(Job).filter(Job.job_id == job_id).first().status)


# ── 孤儿对账 ────────────────────────────────────────────────────────────


def test_pending_job_without_driver_is_reaped(db_session):
    """runner 没起来的 pending 作业：过短闸即判失联，token 一并作废。"""
    _seed(db_session, "job_dead", status="pending", quiet_min=10)

    assert asyncio.run(jr.reap_orphan_jobs()) == 1
    assert _status(db_session, "job_dead") == "interrupted"
    with db_session() as db:
        row = db.query(Job).filter(Job.job_id == "job_dead").first()
        assert "token" not in (row.extra_data or {})  # 残留进程回来也写不动了
        assert "失联" in (row.error_message or "")
        assert "resume" in (row.error_message or "")  # 错误信息必须自带下一步


def test_recent_pending_job_is_left_alone(db_session):
    """刚提交的作业不能被误杀 —— 沙箱冷启动本来就要花点时间。"""
    _seed(db_session, "job_young", status="pending", quiet_min=1)

    assert asyncio.run(jr.reap_orphan_jobs()) == 0
    assert _status(db_session, "job_young") == "pending"


def test_running_job_uses_the_longer_silence_window(db_session):
    """running 用与驱动同一把静默闸：单项耗时很长但确实在跑的作业不该被收。"""
    _seed(db_session, "job_slow", status="running", quiet_min=8)
    assert asyncio.run(jr.reap_orphan_jobs()) == 0
    assert _status(db_session, "job_slow") == "running"

    _seed(db_session, "job_silent", status="running", quiet_min=20)
    assert asyncio.run(jr.reap_orphan_jobs()) == 1
    assert _status(db_session, "job_silent") == "interrupted"


def test_job_driven_in_this_process_is_never_reaped(db_session, monkeypatch):
    """本进程还在驱动的作业归 drive() 管，对账绝不能插手（护栏重复 = 误杀）。"""
    _seed(db_session, "job_live", status="pending", quiet_min=30)

    async def _run():
        task = asyncio.create_task(asyncio.sleep(5))
        jr._active_jobs["job_live"] = task
        try:
            return await jr.reap_orphan_jobs()
        finally:
            task.cancel()

    assert asyncio.run(_run()) == 0
    assert _status(db_session, "job_live") == "pending"


# ── 回调地址探测 ────────────────────────────────────────────────────────


def test_callback_candidates_prefer_same_network_service_name(monkeypatch):
    monkeypatch.delenv("JOB_CALLBACK_URL", raising=False)
    monkeypatch.setenv("PORT", "8011")

    candidates = jr.callback_base_candidates()

    assert candidates[0] == "http://backend:8011"
    # 宿主别名仍在表里兜底，但不再是唯一选项（写死它正是这次事故的根因）
    assert any("host.docker.internal" in c for c in candidates)


def test_explicit_env_wins_and_skips_probing(monkeypatch):
    monkeypatch.setenv("JOB_CALLBACK_URL", "http://custom:9000/api/")

    async def _boom(*a, **k):  # 手动指定即信任，不该再去沙箱里探
        raise AssertionError("probe must not run when JOB_CALLBACK_URL is set")

    monkeypatch.setattr(jr, "_sbx_bash", _boom)

    assert asyncio.run(jr.resolve_callback_base(session_id="s", user_id="u")) == (
        "http://custom:9000/api"
    )


def test_probe_picks_the_reachable_base(monkeypatch):
    monkeypatch.delenv("JOB_CALLBACK_URL", raising=False)
    monkeypatch.setattr(jr, "_resolved_callback_base", None, raising=False)

    async def fake_bash(cmd, *, session_id, user_id, timeout=60):
        return 0, "PICK http://backend:8011\n", ""

    monkeypatch.setattr(jr, "_sbx_bash", fake_bash)

    assert asyncio.run(jr.resolve_callback_base(session_id="s", user_id="u")) == (
        "http://backend:8011"
    )


def test_unreachable_callback_refuses_to_start(monkeypatch):
    """一个都不通就必须当场失败。

    "启动成功但永远没有进度"是最贵的失败形态：用户会等上几个小时才发现什么都没发生。
    """
    monkeypatch.delenv("JOB_CALLBACK_URL", raising=False)
    monkeypatch.setattr(jr, "_resolved_callback_base", None, raising=False)

    async def fake_bash(cmd, *, session_id, user_id, timeout=60):
        return 0, "NONE\n", ""

    monkeypatch.setattr(jr, "_sbx_bash", fake_bash)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(jr.resolve_callback_base(session_id="s", user_id="u"))
    assert "JOB_CALLBACK_URL" in str(exc.value)  # 报错要带修法


# ── 时间戳时区 ──────────────────────────────────────────────────────────


def test_job_timestamp_defaults_are_timezone_aware():
    """naive UTC 写进 timestamptz 会被按会话时区解释 —— 容器是 +08，作业一落库就"早 8 小时"。

    断言落在**默认值本身**上而不是落库后读回的值：SQLite 没有带时区的存储类型，读回一律
    是 naive，用它做判据等于什么都没锁。真正决定行为的是写进去的那个值带不带 tzinfo。
    """
    from core.db.models.job import _utcnow

    now = _utcnow()
    assert now.tzinfo is not None
    assert abs((now - datetime.now(timezone.utc)).total_seconds()) < 60

    for table, columns in (
        (Job.__table__, ("created_at", "updated_at")),
        (Job.__table__.metadata.tables["job_items"], ("updated_at",)),
        (Job.__table__.metadata.tables["job_calls"], ("created_at",)),
    ):
        for name in columns:
            col = table.c[name]
            assert col.default is not None, name
            # 比对行为而不是函数身份：models 包在不同 import 路径下会有各自的模块实例，
            # `is` 比较会假阴性。真正要锁的是"默认值算出来带 tzinfo"。
            assert col.default.arg(None).tzinfo is not None, name
            if col.onupdate is not None:
                assert col.onupdate.arg(None).tzinfo is not None, name
