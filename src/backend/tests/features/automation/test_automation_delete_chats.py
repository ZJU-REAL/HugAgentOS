"""Deleting a scheduled task retires the conversations that only exist under it.

Kept, the scheduled-task sidebar went on listing the deleted task (it groups chats by
their ``automation_task_id``) and opening it answered 404.
"""
from core.db.models import ChatSession, UserShadow
from features.automation.models import ScheduledTaskRun
from features.automation.service import AutomationService


def test_delete_task_soft_deletes_its_run_and_setup_chats_only(db_session):
    db_session.add(UserShadow(user_id="u1", username="u1"))
    db_session.commit()
    svc = AutomationService(db_session)
    task = svc.create_task(user_id="u1", task_type="prompt", prompt="run",
                           cron_expression="30 10 29 6 *")
    other = svc.create_task(user_id="u1", task_type="prompt", prompt="run",
                            cron_expression="30 10 29 6 *")
    db_session.add_all([
        ChatSession(chat_id="chat_run", user_id="u1",
                    extra_data={"automation_task_id": task.task_id, "automation_run": True}),
        ChatSession(chat_id="chat_setup", user_id="u1",
                    extra_data={"automation_task_id": task.task_id}),
        ChatSession(chat_id="chat_other_task", user_id="u1",
                    extra_data={"automation_task_id": other.task_id, "automation_run": True}),
        ChatSession(chat_id="chat_plain", user_id="u1", extra_data={}),
        ScheduledTaskRun(run_id="run_1", task_id=task.task_id, chat_id="chat_run"),
    ])
    db_session.commit()

    assert svc.delete_task(task.task_id, "u1") is True

    db_session.expire_all()
    deleted = {
        row.chat_id for row in db_session.query(ChatSession).filter(ChatSession.deleted_at.isnot(None))
    }
    assert deleted == {"chat_run", "chat_setup"}
