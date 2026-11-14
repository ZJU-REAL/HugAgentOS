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
        original = identity_config.locked_agent
        def edit_before_lock(session, uid):
            # The owner pauses the schedule after the scheduler found the agent due and before it locks the row.
            monkeypatch.setattr(identity_config, "locked_agent", original)
            schedules.update_schedule(session, uid, item["id"], {"enabled": False})
            return original(session, uid)
        monkeypatch.setattr(identity_config, "locked_agent", edit_before_lock)
        assert schedules.fire_due(db, now=4102444800) == 0
        assert svc.tasks.list() == []


# ---------------------------------------------------------------- the scheduler's indexed column

def column(db, uid="owner"):
    from sqlalchemy import select
    from personal_agent.models import PersonalAgent
    db.expire_all()
    return db.scalar(select(PersonalAgent.next_due_at).where(PersonalAgent.user_id == uid))


def test_the_earliest_due_time_is_derived_from_schedules_and_the_proactive_check():
    item = lambda when, **extra: {"id": "s", "next_run": when, **extra}
    assert schedules.due_at({}) is None and schedules.due_at(None) is None
    assert schedules.due_at({"schedules": [item(30.0), item(20.0), item(10.0, enabled=False), item(None)]}) == 20.0
    # The proactive check counts once the agent is scheduled for it.
    assert schedules.due_at({"schedules": [item(20.0)], "heartbeat": {"next_run": 5.0}}) == 20.0
    assert schedules.due_at({"schedules": [item(20.0)], "heartbeat": {"next_run": 5.0, "proactive": {"seeded": True}}}) == 5.0
    assert schedules.due_at({"heartbeat": {"next_run": None, "proactive": {"seeded": True}}}) is None


def test_next_due_at_follows_every_change_of_a_schedule(sessions, monkeypatch):
    monkeypatch.setattr("personal_agent.scheduling.heartbeat.seed_if_new", lambda *args, **kwargs: False)    # schedules alone
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        assert column(db) is None
        daily = schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        first = column(db)
        assert first is not None and first == svc.identity.agent().config["schedules"][0]["next_run"]
        once = schedules.create(db, "owner", "Call", "Call Bob", run_at="2099-01-01T08:00", timezone="UTC")
        assert column(db) == first
        schedules.update_schedule(db, "owner", daily["id"], {"enabled": False})
        assert column(db) == 4070937600.0                                    # only the one-time task is left
        schedules.update_schedule(db, "owner", daily["id"], {"enabled": True, "cron": "0 9 * * *"})
        assert column(db) == first + 3600
        schedules.delete(db, "owner", daily["id"])
        assert column(db) == 4070937600.0
        # Fired: a one-time task is spent, and nothing is due any more.
        assert schedules.fire_due(db, now=4070937601) == 1 and column(db) is None
        assert schedules.fire_due(db, now=4102444800) == 0
        assert once["id"] and schedules.listing(svc.identity.agent().config) == []


def test_a_recurring_schedule_moves_the_column_to_its_next_time(sessions, monkeypatch):
    monkeypatch.setattr("personal_agent.scheduling.heartbeat.seed_if_new", lambda *args, **kwargs: False)    # schedules alone
    with sessions() as db:
        AgentContext(db, "owner").identity.ensure()
        schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        assert schedules.fire_due(db, now=4102444800) == 1                   # 2100-01-01 00:00 UTC
        assert column(db) == 4102444800 + 8 * 3600
        assert schedules.fire_due(db, now=4102444800 + 60) == 0


def test_the_scheduler_reads_only_the_agents_the_column_names(sessions, monkeypatch):
    from personal_agent.models import PersonalAgent
    monkeypatch.setattr("personal_agent.scheduling.heartbeat.seed_if_new", lambda *args, **kwargs: False)    # schedules alone
    with sessions() as db:
        AgentContext(db, "owner").identity.ensure()
        AgentContext(db, "other").identity.ensure()
        schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        looked = []
        fire = schedules.fire_schedules
        monkeypatch.setattr(schedules, "fire_schedules", lambda db, uid, now: looked.append(uid) or fire(db, uid, now))
        # Nothing is due before the time; at the time only the owner with a due schedule is read.
        assert schedules.fire_due(db, now=column(db) - 1) == 0 and looked == []
        assert schedules.fire_due(db, now=4102444800) == 1 and looked == ["owner"]
        # A row without the column is not seen (a database that got the column without the migration's
        # backfill); the one pass at start gives it back.
        row = db.query(PersonalAgent).filter(PersonalAgent.user_id == "owner").one()
        row.next_due_at = None
        db.commit()
        assert schedules.fire_due(db, now=4102444800 + 2 * 86400) == 0
        assert schedules.backfill(db) == 1 and schedules.backfill(db) == 0
        assert schedules.fire_due(db, now=4102444800 + 2 * 86400) == 1


def test_a_column_that_came_apart_from_the_config_is_put_right(sessions, monkeypatch):
    """Review C2: a write that did not go through schedules.store leaves the column early or late."""
    from personal_agent.models import PersonalAgent
    from personal_agent.scheduling import heartbeat
    monkeypatch.setattr("personal_agent.scheduling.heartbeat.seed_if_new", lambda *args, **kwargs: False)

    def set_column(db, value):
        db.query(PersonalAgent).filter(PersonalAgent.user_id == "owner").one().next_due_at = value
        db.commit()

    with sessions() as db:
        AgentContext(db, "owner").identity.ensure()
        schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        right = column(db)
        looked = []
        fire = schedules.fire_schedules
        monkeypatch.setattr(schedules, "fire_schedules", lambda db, uid, now: looked.append(now) or fire(db, uid, now))
        # Early: the row is read once, put right, and not read again before its time.
        set_column(db, 5.0)
        assert [schedules.fire_due(db, now=right - 3600 + 15 * n) for n in range(5)] == [0] * 5
        assert column(db) == right and looked == [right - 3600]
        # The same with a proactive check that is not due and no schedule at all.
        schedules.update_timed(db, "owner", lambda config: config.update(schedules=[],
            heartbeat={"next_run": right + 7200, "proactive": {"seeded": True, "listed": True}}))
        set_column(db, 5.0)
        assert heartbeat.fire_due(db, right - 3600) == 0 and column(db) == right + 7200
        # Late: the scheduler cannot see it; the pass at the start of a process puts it right. Nothing: the column is emptied.
        set_column(db, right + 30 * 86400)
        assert schedules.backfill(db) == 1 and column(db) == right + 7200 and schedules.backfill(db) == 0
        schedules.update_timed(db, "owner", lambda config: config.update(heartbeat={}))
        set_column(db, 5.0)
        assert schedules.backfill(db) == 1 and column(db) is None


def test_any_write_of_the_config_keeps_the_column_with_it(sessions, monkeypatch):
    """End-to-end finding 13: a column later than the config is found by no sweep. So no write of the
    config can leave it behind, whether or not it goes through schedules.store."""
    from personal_agent.identity.config import update_config
    from personal_agent.models import PersonalAgent
    monkeypatch.setattr("personal_agent.scheduling.heartbeat.seed_if_new", lambda *args, **kwargs: False)
    with sessions() as db:
        AgentContext(db, "owner").identity.ensure()
        schedules.create(db, "owner", "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        right = column(db)
        # A writer that knows nothing of schedules moves a time (or writes back the config it read earlier).
        def earlier(config): config["schedules"] = [{**item, "next_run": right - 7200} for item in config["schedules"]]
        update_config(db, "owner", earlier)
        assert column(db) == right - 7200
        row = db.query(PersonalAgent).filter(PersonalAgent.user_id == "owner").one()
        row.config = {**row.config, "schedules": [], "name": "x"}
        db.commit()
        assert column(db) is None
        assert PersonalAgent(config={"schedules": [{"next_run": 9.0}]}).next_due_at == 9.0


def test_an_agent_whose_scheduling_raises_is_not_tried_in_every_sweep(sessions, monkeypatch):
    """Review C2: one broken agent is put off a few minutes; the others are served in the same sweep."""
    from personal_agent.scheduling import heartbeat
    monkeypatch.setattr("personal_agent.scheduling.heartbeat.seed_if_new", lambda *args, **kwargs: False)
    with sessions() as db:
        for uid in ("owner", "other"):
            AgentContext(db, uid).identity.ensure()
            schedules.create(db, uid, "Mail", "Read mail", cron="0 8 * * *", timezone="UTC")
        due = column(db)
        raised = []

        def broken(db, uid, now, hold=None):
            if uid != "owner": return False
            raised.append(now)
            raise RuntimeError("statement timeout")
        monkeypatch.setattr(heartbeat, "fire_one", broken)
        started = [schedules.sweep(db, due + 15 * n)[0] for n in range(40)]
        # Both schedules fired; the broken agent was looked at again only after ERROR_RETRY_SEC, twice in ten minutes.
        assert sum(started) == 2 and raised == [due, due + 300]
        assert column(db, "owner") == due + 300 + schedules.ERROR_RETRY_SEC


def test_proactive_checks_have_no_loop_of_their_own():
    import inspect
    from personal_agent import background
    from personal_agent.scheduling import heartbeat
    assert not hasattr(background.BackgroundServices, "beat_loop") and not hasattr(heartbeat, "seed_new")
    assert not hasattr(background, "HEARTBEAT_INTERVAL_SEC")
    # Their upkeep rides on the maintenance loop; seeding happens once at start.
    upkeep = inspect.getsource(background.BackgroundServices.maintenance_loop)
    assert "proactive_tasks.expire" in upkeep and "heartbeat.cleanup" in upkeep
    start = inspect.getsource(background.BackgroundServices.prepare_schedules)
    assert "schedules.backfill" in start and "heartbeat.seed_all" in start and "self.every" not in start


@pytest.mark.parametrize("filename", [
    "src/backend/alembic/versions/paproactive01_next_due_at.py",
    "ce/overlay/src/backend/alembic/versions/ce_0041_personal_next_due_at.py",
])
def test_the_migration_adds_the_indexed_column_and_fills_it(filename):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import JSON, bindparam, create_engine, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("due_migration", Path(__file__).resolve().parents[3] / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BATCH = 2
    configs = {"a_none": {}, "b_schedule": {"schedules": [{"id": "s1", "next_run": 300.0}, {"id": "s2", "next_run": 200.0},
                                                          {"id": "s3", "next_run": 100.0, "enabled": False}]},
               "c_unseeded": {"heartbeat": {"enabled": True, "next_run": 50.0}},
               "d_seeded": {"schedules": [{"id": "s1", "next_run": 300.0}], "heartbeat": {"next_run": 250.0, "proactive": {"seeded": True}}},
               "e_spent": {"schedules": [{"id": "s1", "next_run": None}]}}
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE personal_agents (id TEXT PRIMARY KEY, config JSON NOT NULL)"))
        insert = text("INSERT INTO personal_agents (id, config) VALUES (:i, :c)").bindparams(bindparam("c", type_=JSON()))
        for ident, config in configs.items(): conn.execute(insert, {"i": ident, "c": config})
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            # Again on a database that already has the column (create_all made it): nothing breaks.
            module.upgrade()
            assert "next_due_at" in {c["name"] for c in inspect(conn).get_columns("personal_agents")}
            assert [index["column_names"] for index in inspect(conn).get_indexes("personal_agents")] == [["next_due_at"]]
            assert dict(conn.execute(text("SELECT id, next_due_at FROM personal_agents")).all()) == {
                "a_none": None, "b_schedule": 200.0, "c_unseeded": None, "d_seeded": 250.0, "e_spent": None}
            # The frozen rule of the revision is the rule the scheduler uses.
            assert all(module.due_at(config) == schedules.due_at(config) for config in configs.values())
            module.downgrade()
            assert "next_due_at" not in {c["name"] for c in inspect(conn).get_columns("personal_agents")}



def test_schedule_instruction_is_hidden_and_its_result_names_the_schedule(sessions):
    from personal_agent.conversation.attention import search
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        svc.conversation.append(svc.identity.agent(), "user", "你好", None, "own:1")
        schedules.create(db, "owner", "早报", "整理今日资讯", cron="0 8 * * *", timezone="UTC")
        assert schedules.fire_due(db, now=4102444800) == 1
        task = svc.tasks.list()[0]
        assert task["goal"].endswith("整理今日资讯")
        rows = svc.conversation.list()
        assert [row["content"] for row in rows] == ["你好"]
        assert search(svc, "整理今日资讯")["messages"] == []
        svc.conversation.append(svc.identity.agent(), "assistant", "今日三条动态", task["id"])
        db.commit()
        rows = svc.conversation.list()
        assert [(row["content"], row["schedule_title"]) for row in rows] == [("你好", None), ("今日三条动态", "早报")]
