"""作业唤醒轮必须继承会话的工作流模式。

背景（线上实测）：一次 265 项的批量作业跑完（265/265 全成），唤醒轮却把整件事搞砸了。
唤醒消息明明写着"可以用 run_job(action='resume') 续跑 / 读取作业结果完成最终交付"，
模型回过头看自己的工具面，却找不到 run_job——因为 `_enqueue_followup_run` 从零搭
context，没带 `workflow_chat`，而 `workflow_mode` 正是 run_job 注册与批量提示词注入
的唯一开关（见 agent_factory 的 Phase 3.85）。

后果不是"少一个工具"这么轻：模型只能弃用作业面，把 265 个工作项拖回主循环逐项重做
（每轮重发全部历史，成本随进度二次方增长），后台作业卡片也随之消失——用户看到的是
"任务从后台掉回前台"。

所以这里锁的是这条不变量：唤醒轮的 context 必须如实反映会话元数据里的
`workflow_chat`，进度唤醒和终态唤醒两条路都要带。
"""

import asyncio
from typing import Any, Dict, List

import core.db.engine as db_engine
import pytest
from core.db.engine import Base
from core.db.models import ChatSession, Job
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)

    from orchestration import job_wakeup

    monkeypatch.setattr(job_wakeup, "SessionLocal", Session, raising=False)
    return Session


def _seed(db_session, *, chat_meta: Dict[str, Any], status: str = "completed") -> None:
    """一条会话 + 一条 wait=False 提交、已进终态的作业。"""
    with db_session() as db:
        db.add(
            ChatSession(
                chat_id="chat_1",
                user_id="u1",
                title="批量分类",
                extra_data=chat_meta,
            )
        )
        db.add(
            Job(
                job_id="job_1",
                user_id="u1",
                chat_id="chat_1",
                name="问答记录逐行分类",
                status=status,
                script_path="/w/classify.py",
                script_text="x",
                sandbox_session_id="chat_1",
                extra_data={"start_params": {"wake_on_finish": True, "model_name": "m"}},
            )
        )
        db.commit()


def _capture_started_runs(monkeypatch, db_session) -> List[Dict[str, Any]]:
    """把唤醒真正入队的那一发拦下来，只留 context 供断言。"""
    from orchestration import job_wakeup

    started: List[Dict[str, Any]] = []

    def fake_load_session_messages(_svc, _chat_id, _user_id):
        return []

    def fake_resolve_all(_db, _user_id):
        return (None, None, None)

    class _FakeChatService:
        def __init__(self, db):
            self._db = db

        def add_message(self, **kw):
            return None

        def get_session(self, chat_id, user_id):
            return (
                self._db.query(ChatSession)
                .filter(ChatSession.chat_id == chat_id, ChatSession.user_id == user_id)
                .first()
            )

    async def fake_start_run(**kw):
        started.append(kw)
        return "run_1"

    import api.routes.v1.chats as chats_mod
    import core.config.catalog_resolver as resolver_mod
    import core.services.chat_service as chat_service_mod
    from orchestration import chat_run_executor

    monkeypatch.setattr(chats_mod, "_load_session_messages", fake_load_session_messages)
    monkeypatch.setattr(resolver_mod, "resolve_all_runtime_enabled", fake_resolve_all)
    monkeypatch.setattr(chat_service_mod, "ChatService", _FakeChatService)
    monkeypatch.setattr(chat_run_executor, "start_run", fake_start_run)
    # 唤醒文案本身不是本用例的关注点，但它要能生成——JobService 走的是同一个假库
    monkeypatch.setattr(job_wakeup, "SessionLocal", db_session, raising=False)
    return started


def test_finish_wake_inherits_workflow_mode(monkeypatch, db_session):
    """工作流会话跑完作业 → 唤醒轮必须仍然是工作流模式（run_job 才注册得上）。"""
    _seed(db_session, chat_meta={"workflow_chat": True})
    started = _capture_started_runs(monkeypatch, db_session)

    from orchestration import job_wakeup

    assert asyncio.run(job_wakeup.wake_on_job_finish("job_1")) is True
    assert len(started) == 1
    ctx = started[0]["context"]
    assert (
        ctx["workflow_chat"] is True
    ), "唤醒轮丢了 workflow_chat：run_job 不会注册，模型只能把作业拖回主循环重做"


def test_finish_wake_does_not_invent_workflow_mode(monkeypatch, db_session):
    """反向闸：会话本来不是工作流模式，唤醒轮不许凭空把它打开。"""
    _seed(db_session, chat_meta={})
    started = _capture_started_runs(monkeypatch, db_session)

    from orchestration import job_wakeup

    assert asyncio.run(job_wakeup.wake_on_job_finish("job_1")) is True
    assert started[0]["context"]["workflow_chat"] is False


def test_progress_wake_inherits_workflow_mode(monkeypatch, db_session):
    """进度播报走的是同一条入队路径，同样不能把模式丢掉。"""
    _seed(db_session, chat_meta={"workflow_chat": True}, status="running")
    started = _capture_started_runs(monkeypatch, db_session)

    from orchestration import chat_run_executor, job_wakeup

    monkeypatch.setattr(chat_run_executor, "get_active_run_for_chat", lambda *a, **k: None)

    ok = asyncio.run(
        job_wakeup.wake_on_job_progress(
            "job_1", stats={"total": 265, "settled": 100}, budget_left={}, stalled=False
        )
    )
    assert ok is True
    assert started[0]["context"]["workflow_chat"] is True
