"""同会话重复作业的冲突策略 —— 拦下必须是"默认"，不能是"禁令"。

背景：一次事故里智能体被进度唤醒后改了脚本、又交了一份新作业，两份并存同时烧预算、
同时叫醒会话，状态条上也分不清哪份算数。于是加了拦截。

但拦死是另一种错：确实要换新脚本重跑时，工具必须给得出路，而不是让调用方无路可走。
所以这里锁的是三条分支都通：默认拦下并给出路、replace 先停旧再跑新、parallel 放行。
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.db.engine as db_engine
from core.db.engine import Base
from core.db.models import Job


@pytest.fixture()
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    import core.llm.tools.job_tool as jt

    monkeypatch.setattr(jt, "SessionLocal", Session, raising=False)
    return Session


class _Toolkit:
    """够用的假 toolkit：只把注册进来的函数抓出来直接调。"""

    def __init__(self):
        self.fn = None

    def register_tool_function(self, fn):
        self.fn = fn


def _make_tool(monkeypatch, db_session, cancelled: list):
    from core.llm.tools import job_tool

    # 真正启动作业的部分全部打桩：这里只验冲突策略，不碰沙箱
    async def fake_cancel(job_id, *, user_id):
        cancelled.append(job_id)
        with db_session() as db:
            row = db.query(Job).filter(Job.job_id == job_id).first()
            if row:
                row.status = "cancelled"
                db.commit()
        return True

    async def fake_sbx_bash(cmd, *, session_id, user_id, timeout=60):
        import base64

        return 0, base64.b64encode(b"print(1)").decode(), ""

    async def fake_start(**kw):
        return "job_new"

    from orchestration import job_runtime

    monkeypatch.setattr(job_runtime, "cancel_job", fake_cancel)
    monkeypatch.setattr(job_runtime, "_sbx_bash", fake_sbx_bash)
    monkeypatch.setattr(job_runtime, "start_job", fake_start)
    monkeypatch.setattr(job_runtime, "spawn_background", lambda *a, **k: None)

    tk = _Toolkit()
    job_tool.register_run_job(
        tk,
        user_id="u1",
        chat_id="chat_1",
        sandbox_session_id="sbx_1",
        allowed_tools=["internet_search"],
        model_name="m",
        model_provider_id="p",
    )
    return tk.fn


def _seed_live_job(db_session, job_id="job_old", status="running"):
    with db_session() as db:
        db.add(
            Job(
                job_id=job_id,
                user_id="u1",
                chat_id="chat_1",
                name="旧作业",
                status=status,
                script_path="/w/a.py",
                script_text="x",
                sandbox_session_id="sbx_1",
            )
        )
        db.commit()


def _payload(resp):
    """把 ToolResponse 的文本块拼回 JSON —— 块可能是 dict 也可能是带 .text 的对象。"""
    text = ""
    for blk in getattr(resp, "content", []) or []:
        text += blk.get("text", "") if isinstance(blk, dict) else str(getattr(blk, "text", "") or "")
    assert text, f"工具没有返回文本内容: {resp!r}"
    return json.loads(text)


def test_default_blocks_but_hands_back_every_exit(monkeypatch, db_session):
    """默认拦下时必须把出路说全 —— 只说"不行"等于把调用方逼进死胡同。"""
    cancelled: list = []
    fn = _make_tool(monkeypatch, db_session, cancelled)
    _seed_live_job(db_session)

    out = _payload(asyncio.run(fn(action="start", script_path="/w/a.py", name="新作业")))

    assert out["ok"] is False
    assert out["job_id"] == "job_old"
    assert "replace" in out["error"] and "resume" in out["error"] and "parallel" in out["error"]
    assert cancelled == [], "默认分支不该动旧作业"


def test_replace_cancels_old_then_starts(monkeypatch, db_session):
    """换了脚本要重跑 —— 这条路必须真的通，且旧作业确实被停掉。"""
    cancelled: list = []
    fn = _make_tool(monkeypatch, db_session, cancelled)
    _seed_live_job(db_session)

    out = _payload(
        asyncio.run(
            fn(action="start", script_path="/w/a.py", name="新作业", wait=False, on_conflict="replace")
        )
    )

    assert out["ok"] is True and out["job_id"] == "job_new"
    assert cancelled == ["job_old"], "replace 必须先停掉旧作业，否则又是两份并存"


def test_parallel_allows_coexistence(monkeypatch, db_session):
    cancelled: list = []
    fn = _make_tool(monkeypatch, db_session, cancelled)
    _seed_live_job(db_session)

    out = _payload(
        asyncio.run(
            fn(action="start", script_path="/w/a.py", name="新作业", wait=False, on_conflict="parallel")
        )
    )

    assert out["ok"] is True
    assert cancelled == [], "parallel 明确表示两份都要，不许偷偷停掉一份"


def test_no_live_job_starts_normally(monkeypatch, db_session):
    """没有在跑的作业时，冲突策略不该有任何存在感。"""
    cancelled: list = []
    fn = _make_tool(monkeypatch, db_session, cancelled)
    _seed_live_job(db_session, status="completed")

    out = _payload(asyncio.run(fn(action="start", script_path="/w/a.py", wait=False)))
    assert out["ok"] is True and cancelled == []
