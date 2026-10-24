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
from core.chat import service as service_module

import asyncio
from typing import Any, Dict, List

import core.chat.runtime.admission as admission
import core.db.engine as db_engine
import core.runs.runtime as runtime
import core.chat.operations.session_context as chat_session_context
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

        def get_session_with_access(self, chat_id, user_id):
            session = self.get_session(chat_id, user_id)
            return (session, "admin") if session is not None else None

    async def fake_start_run(**kw):
        started.append(kw)
        return "run_1"

    import core.capabilities.catalog.resolver as resolver_mod
    import core.chat.service as chat_service_mod
    pass

    monkeypatch.setattr(chat_session_context, 'load_session_messages', fake_load_session_messages)
    monkeypatch.setattr(resolver_mod, "resolve_all_runtime_enabled", fake_resolve_all)
    monkeypatch.setattr(service_module, 'ChatService', _FakeChatService)
    monkeypatch.setattr(admission, "start_run", fake_start_run)
    return started


@pytest.mark.parametrize("status", ["completed", "interrupted"])
def test_finish_wake_inherits_workflow_mode(monkeypatch, db_session, status):
    """工作流会话跑完作业 → 唤醒轮必须仍然是工作流模式（run_job 才注册得上）。"""
    _seed(db_session, chat_meta={"workflow_chat": True}, status=status)
    started = _capture_started_runs(monkeypatch, db_session)

    import core.chat.runtime.job_wakeup as job_wakeup

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

    import core.chat.runtime.job_wakeup as job_wakeup

    assert asyncio.run(job_wakeup.wake_on_job_finish("job_1")) is True
    assert started[0]["context"]["workflow_chat"] is False


def test_progress_wake_inherits_workflow_mode(monkeypatch, db_session):
    """进度播报走的是同一条入队路径，同样不能把模式丢掉。"""
    _seed(db_session, chat_meta={"workflow_chat": True}, status="running")
    started = _capture_started_runs(monkeypatch, db_session)

    import core.chat.runtime.job_wakeup as job_wakeup

    monkeypatch.setattr(runtime, "get_active_run_for_chat", lambda *a, **k: None)

    ok = asyncio.run(
        job_wakeup.wake_on_job_progress(
            "job_1", stats={"total": 265, "settled": 100}, budget_left={}, stalled=False
        )
    )
    assert ok is True
    assert started[0]["context"]["workflow_chat"] is True
