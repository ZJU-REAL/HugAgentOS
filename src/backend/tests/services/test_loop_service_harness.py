"""LoopService harness 增强回归：启动参数持久化 / steering 队列 / 中断归位。

对应 harness 改造：续跑不丢参（agent_loops.extra_data.start_params）、运行中追加
指令（steering 队列，driver 每轮取走清空）、进程重启后 running 孤儿归位 interrupted。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db.engine import Base
from core.services.loop_service import LoopService


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _mk_loop(db):
    return LoopService(db).create_loop(
        user_id="u1", title="t",
        goal_spec={"objective": "写个页面"},
        budget={},
        chat_id="c1",
    )


def test_start_params_roundtrip(db):
    loop = _mk_loop(db)
    svc = LoopService(db)
    svc.save_start_params(loop.loop_id, {
        "model_name": "m-big",
        "model_provider_id": "prov-1",
        "evaluator_model": None,          # None/空值不落盘
        "worker_max_iters": 20,
        "hitl_enabled": False,
        "chat_mode": "high",
    })
    got = svc.get_start_params(loop.loop_id)
    assert got["model_name"] == "m-big"
    assert got["model_provider_id"] == "prov-1"
    assert got["worker_max_iters"] == 20
    assert got["chat_mode"] == "high"
    assert "evaluator_model" not in got

    # 二次保存整体覆盖（不残留旧键）
    svc.save_start_params(loop.loop_id, {"model_name": "m2"})
    got2 = svc.get_start_params(loop.loop_id)
    assert got2 == {"model_name": "m2"}


def test_start_params_missing_loop(db):
    assert LoopService(db).get_start_params("loop_nope") == {}


def test_steering_queue_consume_clears(db):
    loop = _mk_loop(db)
    svc = LoopService(db)
    assert svc.push_steering(loop.loop_id, "改暗色主题")
    assert svc.push_steering(loop.loop_id, "  标题换成中文  ")
    assert not svc.push_steering(loop.loop_id, "   ")  # 空指令拒绝

    got = svc.consume_steering(loop.loop_id)
    assert got == ["改暗色主题", "标题换成中文"]
    # 取走即清空
    assert svc.consume_steering(loop.loop_id) == []


def test_steering_queue_caps_at_ten(db):
    loop = _mk_loop(db)
    svc = LoopService(db)
    for i in range(14):
        svc.push_steering(loop.loop_id, f"指令{i}")
    got = svc.consume_steering(loop.loop_id)
    assert len(got) == 10
    assert got[0] == "指令4" and got[-1] == "指令13"  # 只留最近 10 条


def test_mark_interrupted_only_running(db):
    loop = _mk_loop(db)
    svc = LoopService(db)
    # created 状态不动
    svc.mark_interrupted(loop.loop_id, reason="重启")
    assert svc.get_loop(loop.loop_id).status == "created"

    svc.mark_running(loop.loop_id)
    svc.mark_interrupted(loop.loop_id, reason="服务重启导致运行中断")
    got = svc.get_loop(loop.loop_id)
    assert got.status == "interrupted"
    assert "重启" in (got.result_summary or "")

    # 终态不被二次改写
    got.status = "completed"
    db.commit()
    svc.mark_interrupted(loop.loop_id)
    assert svc.get_loop(loop.loop_id).status == "completed"
