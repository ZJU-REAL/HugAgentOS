"""Owner-facing continuation of an existing task after its execution ends."""
import pytest
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from personal_agent.tasks.task_threads import history
from personal_agent.conversation.associations import instructions
from tests.test_personal_agent_routing import sessions, personal_agent_on  # noqa: F401


def test_completed_task_accepts_followup_in_same_conversation_once(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Write a report", "initial")
        svc.tasks.complete(task["id"], "Original report", {})
        before = history(svc, task["id"])
        result = admit(svc, "Add a conclusion", "followup", work_item_ids=[task["id"]], intent="update")
        assert result == admit(svc, "Add a conclusion", "followup", work_item_ids=[task["id"]], intent="update")
        [updated] = svc.tasks.list()
        assert updated["id"] == task["id"] and updated["status"] == "queued"
        assert updated["result"] == "Original report"
        after = history(svc, task["id"])
        assert after["chat_id"] == before["chat_id"]
        assert [m["content"] for m in after["messages"]] == ["Write a report", "Original report", "Add a conclusion"]


@pytest.mark.asyncio
async def test_implicit_followup_requires_inspecting_target_before_changing_it(sessions):
    from tests.test_personal_agent_routing import scripted, turn
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Write report", "initial")
        entry = admit(svc, "Add a conclusion", "followup")
    await turn(entry["message_id"], scripted(("pa_task_update", {"task_id": task["id"], "instruction": "Add a conclusion"}), "Checking the report first."))
    with sessions() as db:
        assert instructions(db, task.get("chat_id"), task["id"]) == []
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "Please continue that report", "followup-2")
    await turn(entry["message_id"], scripted(("pa_task_get", {"task_id": task["id"]}), ("pa_task_update", {"task_id": task["id"], "instruction": "Add a conclusion"}), "Continuing the report."))
    with sessions() as db:
        assert instructions(db, task.get("chat_id"), task["id"]) == ["Add a conclusion"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_ended_task_continuation_clears_old_execution_state(sessions, status):
    from personal_agent.models import PersonalWorkItem
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Read a document", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status = status
        row.checkpoint = {"approved_scopes": ["computer:cloud"], "approved_action_payloads": {"a": {}}, "auto_resumes": 2,
                          "task_wait_reason": "old error", "action_receipts": {"a": {"status": "done"}}}
        db.commit()
        admit(svc, "Try the next page", "followup", work_item_ids=[task["id"]], intent="update")
        [updated] = svc.tasks.list()
        assert updated["status"] == "queued"
        assert not row.checkpoint.get("approved_scopes") and not row.checkpoint.get("approved_action_payloads")
        assert row.checkpoint["action_receipts"]["a"]["status"] == "done"


def test_unknown_external_effect_stays_blocked_after_followup(sessions):
    from personal_agent.models import PersonalWorkItem
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Send an email", "initial")
        row = db.get(PersonalWorkItem, task["id"])
        row.status = "failed"
        row.checkpoint = {"action_receipts": {"mail": {"status": "uncertain"}}}
        db.commit()
        admit(svc, "Continue", "followup", work_item_ids=[task["id"]], intent="update")
        [updated] = svc.tasks.list()
        assert updated["status"] == "waiting" and updated["uncertain_actions"] == ["mail"]
        with pytest.raises(ValueError): svc.tasks.retry(task["id"])


@pytest.mark.asyncio
async def test_unrelated_message_does_not_inherit_the_only_existing_task(sessions):
    from tests.test_personal_agent_routing import turn, scripted
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Research travel", "initial")
        entry = admit(svc, "Hello", "hello")
        assert entry["work_item_ids"] == []
    await turn(entry["message_id"], scripted("Hello!"))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        assert instructions(db, task.get("chat_id"), task["id"]) == []
        assert svc.conversation.list()[-1]["work_item_ids"] == []


def test_user_followup_to_scheduled_task_is_not_an_unattended_run(sessions):
    from personal_agent.models import PersonalWorkItem
    from personal_agent.scheduling.schedules import is_unattended_run
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Check mail", "schedule:mail:123")
        svc.tasks.complete(task["id"], "No new mail", {})
        admit(svc, "Change this schedule to 9 AM", "followup", work_item_ids=[task["id"]], intent="update")
        assert not is_unattended_run(db.get(PersonalWorkItem, task["id"]))


@pytest.mark.asyncio
async def test_main_conversation_continues_completed_task_without_creating_another(sessions):
    from tests.test_personal_agent_routing import scripted, turn
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Write report", "initial")
        svc.tasks.complete(task["id"], "Original report", {})
    await turn(execute=scripted("Original report"))
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "Add a conclusion to the report", "followup")
    await turn(entry["message_id"], scripted(("pa_task_get", {"task_id": task["id"]}),
        ("pa_task_update", {"task_id": task["id"], "instruction": "Add a conclusion"}), "Continuing the report."))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        [updated] = svc.tasks.list()
        assert updated["id"] == task["id"] and updated["status"] == "queued"
        assert updated["result"] == "Original report"
        assert svc.conversation.list()[-1]["work_item_ids"] == [task["id"]]
        assert history(svc, task["id"])["messages"][-1]["content"] == "Add a conclusion"
