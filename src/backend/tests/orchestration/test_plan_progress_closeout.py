"""计划栏必须收得了尾 —— 哪怕没有任何一个标签页在看。

线上实测（2026-08-18，HugAgentOS 测试机 chat_20260818_222214_xdpmo8）：一份 5 步的计划，
日志里 ``[update_plan]`` 停在 2/5，作业跑完、交付轮正常完成，**再没有第三次调用**。原因有二：

1. 收尾只写在提示词末尾一句"若你先前用过 update_plan…就顺手收个尾"，模型直接略过；
2. 就算它不收尾，"这一轮结束了"这件事过去只有**前端在流结束时**盖章——而交付轮是后端
   自己发起的，用户没点任何东西，页面可能压根没在跟这条流，于是计划栏永远转圈。

所以这里锁三条不变量：
- ``update_plan`` 的每一次更新都落库（跨轮次、跨刷新都还在）；
- 终态唤醒的提示词把**当前清单原样念给模型**并把收尾排在交付之前；
- 收尾是服务端认定的：本会话还有活着的作业就先不收，没有了就收；收尾只改 settled，
  **不伪造**步骤状态（停在 2/5 的计划收尾后仍是 2/5）。
"""

import core.db.engine as db_engine
import pytest
from core.db.engine import Base
from core.db.models import ChatSession, Job
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

PLAN_5 = {
    "title": "问答记录分类",
    "steps": [
        {"title": "读取表格", "status": "completed"},
        {"title": "写作业脚本", "status": "completed"},
        {"title": "跑批量作业", "status": "in_progress"},
        {"title": "导出结果写 Excel", "status": "pending"},
        {"title": "验证与修正", "status": "pending"},
    ],
}


@pytest.fixture()
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)

    import core.chat.plan_progress as pp_mod
    from orchestration import job_wakeup, workflow

    monkeypatch.setattr(pp_mod, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(job_wakeup, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(workflow, "SessionLocal", Session, raising=False)

    with Session() as db:
        db.add(ChatSession(chat_id="chat_1", user_id="u1", title="批量分类", extra_data={}))
        db.commit()
    return Session


def test_plan_survives_the_turn_it_was_created_in(db_session):
    """计划落库 → 换一轮（换一个进程也一样）还读得回来。"""
    from core.chat.plan_progress import load_plan_progress, save_plan_progress

    save_plan_progress("chat_1", PLAN_5)
    got = load_plan_progress("chat_1")
    assert got is not None
    assert [s["status"] for s in got["steps"]] == [
        "completed",
        "completed",
        "in_progress",
        "pending",
        "pending",
    ]
    assert got["settled"] is False


def test_settle_does_not_fake_completion(db_session):
    """收尾只表示"没有下一轮了"，绝不能把没做的步骤改成 completed。"""
    from core.chat.plan_progress import load_plan_progress, save_plan_progress, settle_plan_progress

    save_plan_progress("chat_1", PLAN_5)
    settle_plan_progress("chat_1")
    got = load_plan_progress("chat_1")
    assert got["settled"] is True
    assert [s["status"] for s in got["steps"]] == [
        "completed",
        "completed",
        "in_progress",
        "pending",
        "pending",
    ], "收尾把步骤状态改了 —— 计划栏会谎报完成"


def test_new_user_turn_clears_the_plan(db_session):
    """用户开了新一轮 → 上一段任务的计划栏作废，不该在新任务上空转。"""
    from core.chat.plan_progress import clear_plan_progress, load_plan_progress, save_plan_progress

    save_plan_progress("chat_1", PLAN_5)
    clear_plan_progress("chat_1")
    assert load_plan_progress("chat_1") is None


def test_live_job_blocks_settling(db_session):
    """作业还在后台跑 → 本轮结束不收尾（交付轮还会来更新它）。"""
    from orchestration.workflow import _chat_has_live_job

    assert _chat_has_live_job("chat_1") is False
    with db_session() as db:
        db.add(
            Job(
                job_id="job_1",
                user_id="u1",
                chat_id="chat_1",
                name="分类",
                status="running",
                script_path="/w/x.py",
                script_text="x",
                sandbox_session_id="chat_1",
            )
        )
        db.commit()
    assert _chat_has_live_job("chat_1") is True

    with db_session() as db:
        db.query(Job).filter(Job.job_id == "job_1").update({"status": "completed"})
        db.commit()
    assert _chat_has_live_job("chat_1") is False


def test_finish_wake_prompt_reads_the_checklist_back(db_session):
    """终态唤醒必须把清单原样念给模型，并把收尾排在交付之前。"""
    from core.chat.plan_progress import save_plan_progress
    from core.db.models import Job as JobModel
    from orchestration.job_wakeup import _wake_prompt

    save_plan_progress("chat_1", PLAN_5)
    from core.chat.plan_progress import load_plan_progress

    job = JobModel(job_id="job_1", name="问答记录分类", status="completed")
    prompt = _wake_prompt(job, {"total": 265, "done": 265, "remaining": 0}, load_plan_progress("chat_1"))

    assert "update_plan" in prompt
    assert "跑批量作业" in prompt and "in_progress" in prompt, "清单没念出来，模型只能凭记忆重建"
    # 收尾要排在交付指令之前
    assert prompt.index("update_plan") < prompt.index("最终交付")


def test_finished_plan_is_not_nagged_about(db_session):
    """清单已经全部结算（或压根没用过计划栏）→ 别在唤醒轮里凭空要求它调 update_plan。"""
    from core.db.models import Job as JobModel
    from orchestration.job_wakeup import _wake_prompt

    job = JobModel(job_id="job_1", name="问答记录分类", status="completed")
    done_plan = {"title": "x", "steps": [{"title": "a", "status": "completed"}]}
    assert "update_plan" not in _wake_prompt(job, {"remaining": 0}, done_plan)
    assert "update_plan" not in _wake_prompt(job, {"remaining": 0}, None)
