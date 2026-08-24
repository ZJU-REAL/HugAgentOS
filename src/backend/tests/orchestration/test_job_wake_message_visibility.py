"""唤醒指令是写给模型的，不是写给用户看的。

线上现象：批量作业跑到一半，会话里会多出一条以**用户口吻**贴出来的内部提示词——
「[系统] 进度播报：……**请只用一两句话把上面的进度转述给用户**，然后结束本轮回复。
本轮的硬性边界：不要重复提交作业……」。它在刷新页面后尤其刺眼：唤醒轮跑的时候用户
看到的只是助手那句转述，一刷新，提示词原文就从库里被拉出来贴在对话里了。

模型必须看得见这条消息（历史走 ``load_session_history``），用户不该看见（消息列表接口）。
所以这里锁两条：

1. 落库时带 ``extra_data.hidden_in_chat``，消息列表接口据此把它滤掉；
2. 标记上线前就已经躺在库里的老消息，靠开头前缀兜底，同样滤掉。

另外锁一条唤醒文案：作业丢后台之后没有任何一轮会再动 ``update_plan`` 的计划清单，
终态交付那一轮必须被要求给它收尾——否则计划栏永远停在"提交作业"那一步。
"""

import asyncio

import core.db.engine as db_engine
import pytest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatSession, Job
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


def _seed(db_session, *, status: str = "completed") -> None:
    with db_session() as db:
        db.add(
            ChatSession(
                chat_id="chat_1",
                user_id="u1",
                title="批量分类",
                extra_data={"workflow_chat": True},
            )
        )
        db.add(
            Job(
                job_id="job_1",
                user_id="u1",
                chat_id="chat_1",
                name="问答记录分类",
                status=status,
                script_path="/w/classify.py",
                script_text="x",
                sandbox_session_id="chat_1",
                extra_data={"start_params": {"wake_on_finish": True, "model_name": "m"}},
            )
        )
        db.commit()


def _stub_run_dependencies(monkeypatch, db_session) -> None:
    """Keep the test on the durable admission seam while stubbing model launch."""

    class _FakeChatService:
        def __init__(self, db):
            self._db = db

        def get_session(self, chat_id, user_id):
            return (
                self._db.query(ChatSession)
                .filter(ChatSession.chat_id == chat_id, ChatSession.user_id == user_id)
                .first()
            )

    async def fake_start_run(**kw):
        return "run_1"

    import api.routes.v1.chats as chats_mod
    import core.config.catalog_resolver as resolver_mod
    import core.services.chat_service as chat_service_mod
    from orchestration import chat_run_executor

    monkeypatch.setattr(chats_mod, "_load_session_messages", lambda *a, **k: [])
    monkeypatch.setattr(
        resolver_mod, "resolve_all_runtime_enabled", lambda *a, **k: (None, None, None)
    )
    monkeypatch.setattr(chat_service_mod, "ChatService", _FakeChatService)
    monkeypatch.setattr(chat_run_executor, "start_run", fake_start_run)


def test_finish_wake_message_is_marked_hidden(monkeypatch, db_session):
    _seed(db_session)
    _stub_run_dependencies(monkeypatch, db_session)

    from orchestration import job_wakeup

    assert asyncio.run(job_wakeup.wake_on_job_finish("job_1")) is True
    with db_session() as db:
        rows = db.query(ChatMessage).filter(ChatMessage.chat_id == "chat_1").all()
        assert len(rows) == 1
        extra = rows[0].extra_data
    assert extra["hidden_in_chat"] is True, "唤醒指令没打标记 → 刷新后原文贴进对话"
    assert extra["system_wake"] == "job_finish"
    assert extra["_context_item"]["kind"] == "reminder"
    assert extra["_context_item"]["trust"] == "system"


def test_progress_wake_message_is_marked_hidden(monkeypatch, db_session):
    _seed(db_session, status="running")
    _stub_run_dependencies(monkeypatch, db_session)

    from orchestration import chat_run_executor, job_wakeup

    monkeypatch.setattr(chat_run_executor, "get_active_run_for_chat", lambda *a, **k: None)

    ok = asyncio.run(
        job_wakeup.wake_on_job_progress(
            "job_1",
            stats={"total": 265, "settled": 238, "done": 238},
            budget_left={"calls_left": 4762, "seconds_left": 6840},
            stalled=False,
        )
    )
    assert ok is True
    with db_session() as db:
        message = db.query(ChatMessage).filter(ChatMessage.chat_id == "chat_1").one()
        assert message.extra_data["hidden_in_chat"] is True
        assert message.extra_data["system_wake"] == "job_progress"
        assert message.extra_data["_context_item"]["origin"].startswith(
            "harness:job_wakeup"
        )


class _Msg:
    def __init__(self, role, content, extra_data=None):
        self.role = role
        self.content = content
        self.extra_data = extra_data or {}


def test_message_list_filters_wake_messages():
    """消息列表接口的过滤谓词：带标记的、以及标记上线前落库的老消息，都不进聊天记录。"""
    from api.routes.v1.chats import _is_internal_message

    assert _is_internal_message(_Msg("user", "转述一下", {"hidden_in_chat": True}))
    # 老消息（没有标记，只有开头那句）
    assert _is_internal_message(
        _Msg("user", "[系统] 进度播报：你提交的批量作业「x」仍在后台运行。")
    )
    assert _is_internal_message(_Msg("user", "[系统] 你先前提交的批量作业「x」已结束。"))
    # 真正的用户消息与助手回复一律照常显示
    assert not _is_internal_message(_Msg("user", "帮我把这批问答记录分类"))
    assert not _is_internal_message(_Msg("assistant", "[系统] 进度播报：……"))


def test_finish_wake_prompt_has_no_plan_noise_without_a_plan():
    """没有计划清单的会话，唤醒轮不该凭空提 update_plan。

    有清单时"把清单念回去、要求收尾"的行为改由服务端快照驱动，锁在
    ``test_plan_progress_closeout.py``——那里连清单内容和先后顺序一起验。
    """
    from core.db.models import Job as JobModel
    from orchestration.job_wakeup import _wake_prompt

    job = JobModel(job_id="job_1", name="问答记录分类", status="completed")
    prompt = _wake_prompt(job, {"total": 265, "done": 265, "remaining": 0})
    assert "update_plan" not in prompt
    assert "最终交付" in prompt
