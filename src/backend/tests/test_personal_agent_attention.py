import pytest
from tests.test_personal_agent import sessions, personal_agent_on
from personal_agent.context import AgentContext


def test_inflight_action_is_not_a_user_confirmation(sessions):
    from personal_agent.models import PersonalWorkItem
    from personal_agent.tasks.approvals import reconcile
    with sessions() as db:
        svc = AgentContext(db, "owner")
        item = svc.tasks.submit("Use computer", "inflight-action")
        row = db.get(PersonalWorkItem, item["id"])
        row.status = "running"
        row.checkpoint = {"action_receipts": {"action-1": {"status": "uncertain"}}}
        db.commit()
        assert svc.tasks.list()[0]["uncertain_actions"] == []
        assert row.unconfirmed()  # Internal crash/retry protection is still active.
        from personal_agent.tasks.worker import interrupt
        interrupt(db, row, "Worker interrupted before receiving result")
        db.commit()
        assert row.status == "waiting"
        assert svc.tasks.list()[0]["uncertain_actions"] == ["action-1"]
        with pytest.raises(ValueError, match="Check uncertain"):
            svc.tasks.retry(row.id)
        reconcile(svc, row.id, "action-1", "executed")
        assert svc.tasks.list()[0]["uncertain_actions"] == []
