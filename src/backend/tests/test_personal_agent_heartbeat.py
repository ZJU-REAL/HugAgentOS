"""Heartbeat: the agent checks the user's checklist at an interval and is quiet unless something needs the user."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from core.db.models import ChatMessage
from personal_agent.scheduling import heartbeat
from personal_agent.conversation.message_routing import admit
from personal_agent.models import PersonalAgent, PersonalMessageInbox, PersonalWorkItem
from personal_agent.context import AgentContext
from personal_agent.identity.config import update_config
from tests.test_personal_agent import personal_agent_on  # noqa: F401  (module on, user permitted)
from tests.test_personal_agent_routing import result, scripted, sessions, tools_for, turn  # noqa: F401

CHECKLIST = "# HEARTBEAT.md\n\n- 有等待批准超过 2 小时的任务时提醒我\n"
ALL_DAY = {"start": "00:00", "end": "00:00"}


@pytest.fixture
def beats(sessions, monkeypatch, tmp_path):
    """An agent with a workspace, a checklist and a due heartbeat."""
    import core.db.engine
    monkeypatch.setattr(heartbeat, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        agent_id = AgentContext(db, "owner").identity.ensure()["id"]
    # Workspace seeding opens its own sessions. The test database has one shared connection:
    # point them at it only after the agent exists, so their rollbacks cannot undo its creation.
    monkeypatch.setattr(core.db.engine, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    workspace = tmp_path / agent_id / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "HEARTBEAT.md").write_text(CHECKLIST, encoding="utf-8")
    state = {"factory": sessions, "agent_id": agent_id, "workspace": workspace}

    def enable(**options):
        hb = {"enabled": True, "every_minutes": 30, "active_hours": ALL_DAY, "channel_id": None, "next_run": time.time() - 1, **options}
        with sessions() as db: update_config(db, "owner", lambda config: config.update(heartbeat=hb, timezone="UTC"))
    state["enable"] = enable
    enable()
    return state


def config(factory):
    with factory() as db: return db.scalar(select(PersonalAgent.config).where(PersonalAgent.user_id == "owner"))


def beat_rows(factory):
    with factory() as db:
        return db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.request_key.startswith(heartbeat.KEY_PREFIX))).all()


def fire(factory, now=None):
    with factory() as db: return heartbeat.fire_due(db, now)


def assistant_texts(factory):
    with factory() as db: return [m["content"] for m in AgentContext(db, "owner").conversation.list() if m["role"] == "assistant"]


# ---------------------------------------------------------------- settings and time

def test_settings_are_validated_and_merged(beats):
    current = {"channel_ids": ["bot"], "heartbeat": {"enabled": False, "stats": {"quiet": 3}}}
    for bad in ({"unknown": 1}, {"next_run": 1}, {"every_minutes": 14}, {"every_minutes": 1441}, {"enabled": "yes"},
                {"active_hours": {"start": "25:00", "end": "08:00"}}, {"active_hours": {"start": "08:00"}}, {"channel_id": "other"}):
        with pytest.raises(ValueError): heartbeat.validate(bad, current)
    now = 1_000_000.0
    merged = heartbeat.validate({"enabled": True, "every_minutes": 15, "channel_id": "bot"}, current, now)
    assert merged["next_run"] == now + 15 * 60 and merged["stats"] == {"quiet": 3} and merged["channel_id"] == "bot"
    assert heartbeat.validate({"channel_id": ""}, {**current, "heartbeat": merged})["channel_id"] is None
    assert heartbeat.validate({"enabled": False}, {**current, "heartbeat": merged})["next_run"] is None


def test_active_hours_cross_midnight_and_all_day():
    at = lambda hour, minute=0: datetime(2026, 10, 12, hour, minute, tzinfo=timezone.utc).timestamp()
    day = {"active_hours": {"start": "08:00", "end": "22:00"}}
    night = {"active_hours": {"start": "22:00", "end": "06:00"}}
    assert heartbeat.in_active_hours(day, "UTC", at(8)) and not heartbeat.in_active_hours(day, "UTC", at(22))
    assert heartbeat.in_active_hours(night, "UTC", at(23)) and heartbeat.in_active_hours(night, "UTC", at(5, 59))
    assert not heartbeat.in_active_hours(night, "UTC", at(12))
    assert heartbeat.in_active_hours({"active_hours": ALL_DAY}, "UTC", at(3))
    assert heartbeat.next_window_start(day, "UTC", at(23)) == at(8) + 86400
    assert heartbeat.next_window_start(day, "UTC", at(7)) == at(8)


def test_only_real_items_count_as_a_checklist():
    from personal_agent.workspace.constants import PROMPT_DIR
    template = (PROMPT_DIR / "template_heartbeat.system.md").read_text(encoding="utf-8")
    assert heartbeat.checklist_items(template) == ""
    assert heartbeat.checklist_items("# T\n\n- \n* [ ]\n1.\n<!-- - hidden -->\n") == ""
    assert heartbeat.checklist_items(CHECKLIST) == "- 有等待批准超过 2 小时的任务时提醒我"


def test_enabling_writes_the_empty_checklist_once(beats):
    (beats["workspace"] / "HEARTBEAT.md").unlink()
    with beats["factory"]() as db:
        update_config(db, "owner", lambda c: c.pop("heartbeat"))
        AgentContext(db, "owner").identity.configure({"heartbeat": {"enabled": True}})
    written = (beats["workspace"] / "HEARTBEAT.md").read_text(encoding="utf-8")
    assert "心跳清单" in written and heartbeat.checklist_items(written) == ""
    (beats["workspace"] / "HEARTBEAT.md").write_text(CHECKLIST, encoding="utf-8")
    with beats["factory"]() as db: AgentContext(db, "owner").identity.configure({"heartbeat": {"enabled": True, "every_minutes": 60}})
    assert (beats["workspace"] / "HEARTBEAT.md").read_text(encoding="utf-8") == CHECKLIST


# ---------------------------------------------------------------- scheduling

def test_a_due_beat_is_admitted_once_and_missed_slots_collapse(beats):
    beats["enable"](next_run=time.time() - 6 * 3600)
    now = time.time()
    assert fire(beats["factory"], now) == 1
    assert fire(beats["factory"], now) == 0
    [row] = beat_rows(beats["factory"])
    hb = config(beats["factory"])["heartbeat"]
    assert row.payload["kind"] == "heartbeat" and hb["next_run"] == now + 1800 and hb["day"]["beats"] == 1
    with beats["factory"]() as db:
        event = db.get(ChatMessage, row.message_id)
        assert event.role == "system" and event.extra_data["event"] == "heartbeat"
        # The user's view lists only user and assistant messages.
        assert AgentContext(db, "owner").conversation.list() == []


@pytest.mark.parametrize("reason", ["empty_checklist", "outside_hours", "paused", "busy", "daily_limit"])
def test_skips_make_no_beat_and_say_why(beats, reason):
    factory, now = beats["factory"], time.time()
    if reason == "empty_checklist": (beats["workspace"] / "HEARTBEAT.md").write_text("# HEARTBEAT.md\n<!-- - x -->\n", encoding="utf-8")
    if reason == "outside_hours":
        hour = datetime.fromtimestamp(now, timezone.utc).hour
        beats["enable"](active_hours={"start": "%02d:00" % ((hour + 2) % 24), "end": "%02d:00" % ((hour + 3) % 24)})
    if reason == "paused":
        with factory() as db: db.get(PersonalAgent, beats["agent_id"]).status = "paused"; db.commit()
    if reason == "busy":
        with factory() as db: admit(AgentContext(db, "owner"), "你好", "hi")
    if reason == "daily_limit":
        beats["enable"](day={"date": datetime.fromtimestamp(now, timezone.utc).date().isoformat(), "beats": heartbeat.daily_max()})
    assert fire(factory, now) == 0 and beat_rows(factory) == []
    hb = config(factory)["heartbeat"]
    assert hb["last"]["outcome"] == "skipped" and hb["last"]["reason"] == reason
    if reason == "outside_hours": assert hb["next_run"] == heartbeat.next_window_start(hb, "UTC", now)
    elif reason == "busy": assert hb["next_run"] == now + heartbeat.BUSY_RETRY_SEC
    else: assert hb["next_run"] == now + 1800


def test_the_server_switch_stops_all_beats(beats, monkeypatch):
    monkeypatch.setenv("PERSONAL_AGENT_HEARTBEAT", "off")
    assert fire(beats["factory"]) == 0 and beat_rows(beats["factory"]) == []


def test_run_now_ignores_active_hours_but_not_the_rest(beats):
    factory = beats["factory"]
    hour = datetime.now(timezone.utc).hour
    beats["enable"](enabled=False, active_hours={"start": "%02d:00" % ((hour + 2) % 24), "end": "%02d:00" % ((hour + 3) % 24)})
    with factory() as db: started = heartbeat.run_now(db, "owner")
    assert started["started"] and len(beat_rows(factory)) == 1
    with factory() as db, pytest.raises(ValueError, match="Wait a minute"): heartbeat.run_now(db, "owner")
    (beats["workspace"] / "HEARTBEAT.md").write_text("", encoding="utf-8")
    with factory() as db, pytest.raises(ValueError):
        heartbeat.run_now(db, "owner", now=time.time() + 120)


# ---------------------------------------------------------------- the beat turn

async def admitted_beat(beats):
    assert fire(beats["factory"]) == 1
    return beat_rows(beats["factory"])[0].message_id


@pytest.mark.asyncio
async def test_a_quiet_beat_leaves_nothing_for_the_user(beats):
    ident = await admitted_beat(beats)
    await turn(ident, scripted(("pa_stay_quiet", {"reason": "没有待批准任务"})))
    assert assistant_texts(beats["factory"]) == []
    with beats["factory"]() as db: assert db.get(PersonalMessageInbox, ident).decision["quiet"]
    assert config(beats["factory"])["heartbeat"]["last"]["outcome"] == "quiet"


@pytest.mark.asyncio
async def test_an_alert_is_kept_and_the_same_alert_is_not_sent_again(beats):
    ident = await admitted_beat(beats)
    await turn(ident, scripted("「周报」等你批准已超过 3 小时。"))
    with beats["factory"]() as db: [message] = [m for m in AgentContext(db, "owner").conversation.list() if m["role"] == "assistant"]
    assert message["heartbeat"] and config(beats["factory"])["heartbeat"]["stats"] == {"alert": 1}
    ident = await admitted_beat_again(beats)
    await turn(ident, scripted("「周报」等你批准已超过 3 小时！"))
    assert assistant_texts(beats["factory"]) == ["「周报」等你批准已超过 3 小时。"]
    assert config(beats["factory"])["heartbeat"]["last"]["outcome"] == "duplicate"


async def admitted_beat_again(beats):
    """The next slot is due now; the settings and state stay."""
    known = {row.message_id for row in beat_rows(beats["factory"])}
    with beats["factory"]() as db:
        # A new object: an in-place change of the JSON column would not be saved.
        update_config(db, "owner", lambda c: c.update(heartbeat={**c["heartbeat"], "next_run": time.time() - 1}))
    assert fire(beats["factory"]) == 1, config(beats["factory"])["heartbeat"].get("last")
    [ident] = [row.message_id for row in beat_rows(beats["factory"]) if row.message_id not in known]
    return ident


@pytest.mark.asyncio
async def test_the_quiet_token_and_an_unfinished_reply_never_reach_the_user(beats):
    ident = await admitted_beat(beats)
    seen = {}

    async def look(plan):
        with beats["factory"]() as db: seen["messages"] = AgentContext(db, "owner").conversation.list()
    await turn(ident, scripted("HEARTBEAT_OK", look))
    assert seen["messages"] == [] and assistant_texts(beats["factory"]) == []
    assert config(beats["factory"])["heartbeat"]["last"]["outcome"] == "quiet"


@pytest.mark.asyncio
async def test_a_failed_beat_does_not_tell_the_user(beats):
    ident = await admitted_beat(beats)

    async def broken(plan, binding): raise RuntimeError("model offline")
    await turn(ident, broken)
    assert assistant_texts(beats["factory"]) == []
    assert config(beats["factory"])["heartbeat"]["last"]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_a_beat_starts_one_unattended_task_and_its_result_may_stay_quiet(beats):
    from personal_agent.scheduling.schedules import is_unattended_run
    ident = await admitted_beat(beats)
    plan = {}

    async def keep(p): plan.update(p)
    await turn(ident, scripted(("pa_task_create", {"goal": "检查收件箱里的客户邮件", "title": "查邮件"}),
                               ("pa_task_create", {"goal": "再查一次", "title": "再查"}), keep))
    assert plan["results"][1] == {"error": "PermissionError"}
    assert assistant_texts(beats["factory"]) == []
    assert config(beats["factory"])["heartbeat"]["last"]["outcome"] == "delegated"
    with beats["factory"]() as db:
        [task] = db.scalars(select(PersonalWorkItem)).all()
        assert is_unattended_run(task)
        svc = AgentContext(db, "owner"); svc.tasks.complete(task.id, "没有新的客户邮件", {})
        event = db.scalar(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).message_id
    await turn(event, scripted(("pa_stay_quiet", {"reason": "没有新邮件"})))
    assert assistant_texts(beats["factory"]) == []


@pytest.mark.asyncio
async def test_a_beat_cannot_change_the_users_tasks_or_schedules(beats):
    from personal_agent.conversation import front
    from personal_agent.conversation import turn_worker
    ident = await admitted_beat(beats)
    assert turn_worker.claim("w") == ident
    plan = front._prepare(ident, "w")
    token = front.current_turn.set(plan["state"])
    try:
        tools = tools_for(ident)
    finally: front.current_turn.reset(token)
    assert {"pa_stay_quiet", "pa_task_create", "pa_memory_edit"} <= set(tools)
    assert not {"pa_task_update", "pa_task_control", "pa_schedule_create", "pa_schedule_delete", "pa_set_name"} & set(tools)


def test_user_messages_go_first_and_turns_stay_one_at_a_time(beats):
    from personal_agent.conversation import turn_worker
    factory = beats["factory"]
    assert fire(factory) == 1
    beat = beat_rows(factory)[0].message_id
    with factory() as db: message = admit(AgentContext(db, "owner"), "你好", "hi")["message_id"]
    assert turn_worker.claim("w") == message
    # The beat waits while the user's turn holds its lease.
    assert turn_worker.claim("w2") is None
    with factory() as db: db.get(PersonalMessageInbox, message).status = "done"; db.commit()
    assert turn_worker.claim("w2") == beat


def test_a_beat_that_waited_too_long_is_dropped(beats):
    from personal_agent.conversation import turn_worker
    factory = beats["factory"]
    assert fire(factory) == 1
    with factory() as db:
        row = db.scalars(select(PersonalMessageInbox)).one()
        row.created_at = datetime.now(timezone.utc) - timedelta(seconds=heartbeat.STALE_SEC + 60); db.commit()
    assert turn_worker.claim("w") is None
    [row] = beat_rows(factory)
    assert row.status == "done" and row.decision["outcome"] == "stale"
    assert config(factory)["heartbeat"]["last"]["outcome"] == "stale"


@pytest.mark.asyncio
async def test_the_beat_sees_its_rules_and_the_checklist_but_not_earlier_checks(beats):
    from personal_agent.conversation import front
    ident = await admitted_beat(beats)
    await turn(ident, scripted(("pa_stay_quiet", {"reason": "无"})))
    with beats["factory"]() as db: admit(AgentContext(db, "owner"), "我从飞书发来的消息", "im")
    with beats["factory"]() as db: db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).one().status = "done"; db.commit()
    second = await admitted_beat_again(beats)
    prompt = front.system_prompt(second) + front.turn_context(second)
    assert "This turn: a heartbeat check" in prompt and "有等待批准超过 2 小时的任务时提醒我" in prompt
    with beats["factory"]() as db: rows = front.history(db, second)
    texts = [row["content"] for row in rows]
    assert any("我从飞书发来的消息" in text for text in texts)
    assert sum("[Heartbeat check]" in text for text in texts) == 1


@pytest.mark.asyncio
async def test_quiet_beats_are_removed_after_a_day_and_alerts_stay(beats):
    factory = beats["factory"]
    quiet = await admitted_beat(beats)
    await turn(quiet, scripted(("pa_stay_quiet", {"reason": "无"})))
    alert = await admitted_beat_again(beats)
    await turn(alert, scripted("有一个任务等你批准。"))
    with factory() as db:
        for row in db.scalars(select(PersonalMessageInbox)).all(): row.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.commit()
        # Only the quiet check goes; the alert keeps its event, as a task result does.
        assert heartbeat.cleanup(db) == 1
        assert db.scalar(select(PersonalMessageInbox.message_id)) == alert
    assert assistant_texts(factory) == ["有一个任务等你批准。"]


@pytest.mark.asyncio
async def test_alerts_and_scheduled_results_go_to_the_home_bot_in_direct_chats_only(beats):
    factory = beats["factory"]
    direct = {"channel_id": "bot", "channel_type": "lark", "msg": {"text": "hi", "chat_type": "p2p"}, "placeholder_id": None}
    group = {**direct, "msg": {"text": "hi", "chat_type": "group"}}
    with factory() as db:
        update_config(db, "owner", lambda c: c.update(channel_ids=["bot"]))
        svc = AgentContext(db, "owner")
        for key, origin in (("d", direct), ("g", group)):
            entry = admit(svc, "hi", key, extra={"origin": "channel", "channel_origin": origin})
            db.get(PersonalMessageInbox, entry["message_id"]).status = "done"
        db.commit()
    beats["enable"](channel_id="bot")
    ident = await admitted_beat_again(beats)
    await turn(ident, scripted("有一个任务等你批准。"))
    with factory() as db:
        reply = db.scalars(select(ChatMessage).where(ChatMessage.role == "assistant")).one()
        assert reply.extra_data["channel_delivery"]["origin"] == direct
        # A scheduled result has no IM message of its own; it goes home too.
        from personal_agent.scheduling.schedules import SCHEDULE_KEY_PREFIX
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("x" * 9000, SCHEDULE_KEY_PREFIX + "s:1")["id"]
        svc.tasks.complete(task, "长结果" * 3000, {})
        report = db.scalars(select(ChatMessage).where(ChatMessage.role == "assistant").order_by(ChatMessage.chat_seq.desc())).first()
        assert report.extra_data["channel_delivery"]["origin"] == direct
