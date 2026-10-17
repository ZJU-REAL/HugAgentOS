"""Schedule settings through the authenticated API and scheduling boundary."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from personal_agent.api.agent import router; from personal_agent.api.dependencies import service
from personal_agent.context import AgentContext
from personal_agent.scheduling import schedules
from tests.test_personal_agent_routing import sessions, personal_agent_on  # noqa: F401


def client_for(db, uid):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[service] = lambda: AgentContext(db, uid)
    return TestClient(app)


def test_owner_edits_schedule_in_place_and_new_instruction_fires(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        item = schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        with client_for(db, "owner") as client:
            response = client.patch("/v1/personal-agent/schedules/" + item["id"], json={
                "title": "Report", "instruction": "Summarize mail", "cron": "0 9 * * *", "timezone": "Asia/Shanghai"})
        assert response.status_code == 200
        updated = response.json()["data"]
        assert updated["id"] == item["id"] and updated["title"] == "Report"
        assert updated["next_run"].endswith("09:00:00+08:00")
        assert len(schedules.listing(svc.identity.agent().config)) == 1
        assert schedules.fire_due(db, now=4102444800) == 1
        assert svc.tasks.list()[0]["goal"] == "【定时任务：Report，现在执行】Summarize mail"


def test_schedule_edits_are_owner_scoped_and_reject_stale_settings(sessions):
    with sessions() as db:
        for uid in ("owner", "other"): AgentContext(db, uid).identity.ensure()
        item = schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        url = "/v1/personal-agent/schedules/" + item["id"]
        with client_for(db, "other") as client:
            assert client.patch(url, json={"title": "Other"}).status_code == 404
        with client_for(db, "owner") as client:
            assert client.patch(url, json={"title": "New", "revision": 0}).status_code == 200
            assert client.patch(url, json={"title": "Stale", "revision": 0}).status_code == 409
        [current] = schedules.listing(AgentContext(db, "owner").identity.agent().config)
        assert current["title"] == "New" and current["next_run"] == item["next_run"]


@pytest.mark.parametrize("patch", [{"cron": "* * * * *"}, {"timezone": "Unknown/Zone"}, {"run_at": "2000-01-01T00:00"}, {"title": "   "}, {"instruction": None}, {"cron": "0 8 * * *", "run_at": "2099-01-01T08:00"}])
def test_invalid_schedule_update_preserves_saved_settings(sessions, patch):
    with sessions() as db:
        svc = AgentContext(db, "owner"); svc.identity.ensure()
        item = schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        with client_for(db, "owner") as client:
            assert client.patch("/v1/personal-agent/schedules/" + item["id"], json=patch).status_code in (409, 422)
        assert schedules.listing(svc.identity.agent().config) == [item]


def test_paused_schedule_does_not_fire_and_can_be_changed_to_once(sessions):
    with sessions() as db:
        svc = AgentContext(db, "owner"); svc.identity.ensure()
        item = schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        paused = schedules.update_schedule(db, "owner", item["id"], {"enabled": False})
        assert paused["next_run"] is None
        assert schedules.fire_due(db, now=4102444800) == 0
        assert svc.tasks.list() == []
        once = schedules.update_schedule(db, "owner", item["id"], {"enabled": True, "run_at": "2099-01-01T08:00", "timezone": "Asia/Shanghai"})
        assert once["cron"] is None and once["next_run"] == "2099-01-01T08:00:00+08:00"
        assert schedules.fire_due(db, now=4070908801) == 1
        assert schedules.fire_due(db, now=4070908801) == 0
        assert schedules.listing(svc.identity.agent().config) == []


def test_scheduler_reloads_settings_after_scanning_due_owners(sessions, monkeypatch):
    from personal_agent import context as module
    from personal_agent.identity import config as identity_config
    with sessions() as db:
        svc = AgentContext(db, "owner"); svc.identity.ensure()
        item = schedules.create(db, "owner", "Mail", "Old instructions", run_at="2099-01-01T08:00", timezone="UTC")
        original = identity_config.update_config
        def edit_before_lock(session, uid, change):
            monkeypatch.setattr(identity_config, "update_config", original)
            schedules.update_schedule(session, uid, item["id"], {"enabled": False})
            return original(session, uid, change)
        monkeypatch.setattr(identity_config, "update_config", edit_before_lock)
        assert schedules.fire_due(db, now=4102444800) == 0
        assert svc.tasks.list() == []
