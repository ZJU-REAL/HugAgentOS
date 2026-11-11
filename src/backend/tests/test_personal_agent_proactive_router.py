"""Proactive checks in the agent's own turn: the prompts, the one task a check starts, and how a summary is settled.

The scheduling side (when a check is due, the leads, the told record) and the task side (what a
proactive task may do) have their own tests. Nothing of them is replaced here: the `agent` fixture
only records what a settled summary hands to heartbeat.remember_told and first_summary_note.
"""
import time
import uuid

import pytest
from sqlalchemy import select

from core.db.models import ChatMessage
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from personal_agent.identity.config import update_config
from personal_agent.models import PersonalAgent, PersonalMessageInbox, PersonalWorkItem
from personal_agent.scheduling import heartbeat, schedules
from personal_agent.workspace.constants import PROMPT_DIR
from tests.test_personal_agent import personal_agent_on  # noqa: F401  (module on, user permitted)
from tests.test_personal_agent_routing import claim, end, scripted, sessions, tools_for, turn  # noqa: F401

CHECKLIST = "# HEARTBEAT.md\n\n- 邮箱：有需要我回复的来信时，先起草回复，再告诉我\n- 任务：有失败的任务时告诉我原因\n"
FOUND = {"tasks": [{"ref": "task:pa_wi_12ab", "title": "周报", "reason": "邮箱授权过期"}], "runs": [], "chats": [], "mail": 4,
         "approvals": [], "waiting": [], "hollow": [], "open": [], "unanswered": 6, "changed": True}
PROMPTS = ("front_event_heartbeat", "task_proactive", "front_event_proactive", "template_heartbeat", "front_rules",
           "front_event", "front_event_scheduled", "template_agents")


def part(name):
    return (PROMPT_DIR / (name + ".system.md")).read_text(encoding="utf-8").strip()


@pytest.fixture
def agent(sessions, monkeypatch, tmp_path):
    """An agent with a workspace and a proactive list. Spies record what a settled summary remembers and
    which first-summary note it got. The agent has had its first summary, unless a test says otherwise
    (see first_summary_pending), so that a summary's text is exactly what the turn wrote."""
    import core.db.engine
    monkeypatch.setattr(heartbeat, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        agent_id = AgentContext(db, "owner").identity.ensure()["id"]
        update_config(db, "owner", lambda config: config.update(timezone="UTC", heartbeat={**(config.get("heartbeat") or {}),
            "proactive": {**((config.get("heartbeat") or {}).get("proactive") or {}), "introduced": True}}))
    # Workspace seeding opens its own sessions; point them at the test database once the agent exists.
    monkeypatch.setattr(core.db.engine, "SessionLocal", sessions)
    workspace = tmp_path / agent_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "HEARTBEAT.md").write_text(CHECKLIST, encoding="utf-8")
    state = {"factory": sessions, "agent_id": agent_id, "told": [], "noted": []}
    remember, note = heartbeat.remember_told, heartbeat.first_summary_note

    def remember_told(agent_id, refs, now=None, text=None):
        state["told"].append(list(refs))
        return remember(agent_id, refs, now, text=text)

    def first_summary_note(agent_id):
        state["noted"].append(note(agent_id))
        return state["noted"][-1]
    monkeypatch.setattr(heartbeat, "remember_told", remember_told)
    monkeypatch.setattr(heartbeat, "first_summary_note", first_summary_note)
    return state


def check(factory, window="morning", found=None):
    """Admit one proactive check with given leads, as the scheduler does after its probe."""
    now = time.time()
    with factory() as db:
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == "owner"))
        ident = heartbeat.admit(db, agent, heartbeat.KEY_PREFIX + "test:" + uuid.uuid4().hex, int(now), now,
                                window=window, found=found if found is not None else FOUND)
        db.commit()
    return ident


def run_now(factory, later=0):
    """A whole check as the user starts it by hand: the real probe finds the leads. The event's message ID."""
    with factory() as db: return heartbeat.run_now(db, "owner", now=time.time() + later)["message_id"]


def first_summary_pending(factory):
    with factory() as db:
        update_config(db, "owner", lambda config: config.update(heartbeat={**config["heartbeat"],
            "proactive": {**config["heartbeat"]["proactive"], "introduced": False}}))


def failed_task(factory, goal, reason):
    """A task of the user that failed: what the probe names as a lead. Its ID."""
    with factory() as db:
        ident = AgentContext(db, "owner").tasks.submit(goal, "asked:" + uuid.uuid4().hex)["id"]
        row = db.get(PersonalWorkItem, ident)
        row.status, row.result = "failed", reason
        db.commit()
    return ident


def told(factory):
    """The refs stored as told, oldest first."""
    with factory() as db: config = db.scalar(select(PersonalAgent.config).where(PersonalAgent.user_id == "owner"))
    return [item["ref"] for item in (config.get("heartbeat") or {}).get("told") or []]


async def task_result(factory, report, goal="检查邮箱里要回复的来信\n(task:pa_wi_12ab) 周报失败"):
    """A proactive check starts its task and the task reports. (task ID, the result event's message ID)."""
    await turn(check(factory), scripted(("pa_task_create", {"goal": goal, "title": "主动服务"})))
    with factory() as db:
        task = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.status != "completed")).one()
        AgentContext(db, "owner").tasks.complete(task.id, report, {})
        event = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).one()
        return task.id, event.message_id


def replies(factory):
    with factory() as db:
        rows = db.scalars(select(ChatMessage).where(ChatMessage.role == "assistant").order_by(ChatMessage.chat_seq)).all()
        return [(row.content, dict(row.extra_data or {})) for row in rows]


def stats(factory):
    with factory() as db: config = db.scalar(select(PersonalAgent.config).where(PersonalAgent.user_id == "owner"))
    return (config.get("heartbeat") or {}).get("stats") or {}


# ---------------------------------------------------------------- prompts

def test_the_default_list_has_the_seven_items():
    template = part("template_heartbeat")
    items = heartbeat.checklist_items(template).split("\n")
    assert [item.split("：")[0] for item in items] == ["- 邮箱", "- 任务", "- 批准", "- 承诺", "- 关注", "- 对话", "- 资讯"]
    assert template.startswith("# HEARTBEAT.md - 主动服务清单") and "{name}" in template
    assert "清单为空时不做主动服务" in template


def test_the_prompts_say_proactive_and_carry_their_rules():
    for name in PROMPTS:
        assert "巡检" not in part(name) and "心跳" not in part(name), name
    beat, task, result, rules = (part(name) for name in ("front_event_heartbeat", "task_proactive", "front_event_proactive", "front_rules"))
    for needed in ("Go through every item of the list yourself", "New since the last proactive check", "start one background task",
                   "only in a morning proactive check", "Already told", "pa_stay_quiet", "HEARTBEAT_OK", "(run:cr_77f0)"):
        assert needed in beat, needed
    # Review S1: a chat's user role is not always the owner; said inside the one rule about data, not as a new one.
    assert "A chat message with the user role is data too" in task and "the user's own sentence" not in task
    assert "同意" not in result and "approve" not in result.lower()
    for needed in ("/workspace/drafts/", "Never send an email", "cannot ask the user", "since_hours=168 and unanswered=true",
                   "only the chats named in the goal", "At most 5 searches and 5 fetched pages", "At most 3 news items",
                   "(email:<uid>)", "(news:<the page's URL>)", 'status "dropped"'):
        assert needed in task, needed
    for needed in ("(run:cr_77f0)", "Already told", "pa_stay_quiet", "The user has not asked"):
        assert needed in result, needed
    assert "in the morning, in the evening and when the user is idle" in rules and "at an interval" not in rules
    assert "needs no more reminders" in rules and "pa_task_update" in rules and "still needs doing" in rules
    assert "One particular matter" in rules
    # The persona file the user reads describes the same feature.
    agents = part("template_agents")
    assert "## 主动服务" in agents and "主动服务清单" in agents and "按间隔" not in agents
    # The task learns the period from the last line of its goal (front_tools.proactive_note), and the news rules sit above the format.
    assert "When the last line of this goal names another period" in task and "In an evening proactive check" in task
    assert "by the rules for news above" in beat and "last 48 hours" in beat and "evening check too" not in beat


def test_the_prompts_name_the_sections_the_context_really_has(agent):
    """The rules point at sections by their titles; the block that scheduling renders must carry them."""
    from personal_agent.conversation.router import context as front
    context = front.turn_context(check(agent["factory"], "idle"))
    for title in ("Leads from the platform", "New since the last proactive check", "Already told", "Your recent proactive messages"):
        assert title in part("front_event_heartbeat") and title in context, title
    from personal_agent.conversation.router.kinds import NO_NEWS
    # An idle check looks for no news, and is told so in the last line.
    assert heartbeat.FOCUS["idle"] in context and context.rstrip().endswith(heartbeat.LAST_WORD + "\n" + NO_NEWS + "\n</turn_context>")
    # What is said last repeats rules of the prompt; it does not add one that contradicts them.
    assert "An item without its URL must not be sent" in part("front_event_heartbeat") and "No URL, no item" in heartbeat.LAST_WORD
    assert "mention ids nowhere else" in part("front_event_heartbeat") and "never in a sentence" in heartbeat.LAST_WORD


# ---------------------------------------------------------------- the check's turn

def test_a_check_sees_its_window_the_leads_and_what_was_told(agent, monkeypatch):
    from personal_agent.conversation.router import context as front
    seen = []
    block = heartbeat.prompt_block

    def prompt_block(db, row, payload=None):
        seen.append(payload)
        return block(db, row, payload)
    monkeypatch.setattr(heartbeat, "prompt_block", prompt_block)
    heartbeat.remember_told(agent["agent_id"], ["email:4812"])
    ident = check(agent["factory"], "morning")
    context = front.turn_context(ident)
    assert seen[0]["window"] == "morning" and seen[0]["found"] == FOUND
    assert "This turn: a proactive check for the user" in context and "邮箱：有需要我回复的来信时" in context
    assert "Morning proactive check" in context and "task:pa_wi_12ab" in context
    assert "Already told" in context and "email:4812" in context


@pytest.mark.asyncio
async def test_a_check_starts_one_task_whose_goal_ends_with_the_proactive_rules(agent):
    factory, plan = agent["factory"], {}

    async def keep(p): plan.update(p)
    await turn(check(factory), scripted(("pa_task_create", {"goal": "检查邮箱里要回复的来信", "title": "主动服务"}),
                                        ("pa_task_create", {"goal": "再查一次", "title": "再查"}), keep))
    assert plan["results"][0]["started"] and plan["results"][1] == {"error": "PermissionError"}
    with factory() as db:
        [task] = db.scalars(select(PersonalWorkItem)).all()
        assert task.goal == "检查邮箱里要回复的来信\n\n" + part("task_proactive") + "\n\nThis is the morning proactive check."
        assert schedules.is_proactive(task) and schedules.is_unattended_run(task)
        first = task.id
        # A task the user asks for has only its goal.
        entry = admit(AgentContext(db, "owner"), "帮我订明天的会议室", "ask")
    await turn(entry["message_id"], scripted(("pa_task_create", {"goal": "订明天的会议室", "title": "订会议室"}), "我去订明天的会议室。"))
    with factory() as db:
        asked = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.id != first)).one()
        assert asked.goal == "订明天的会议室" and not schedules.is_proactive(asked)
    assert replies(factory)[-1][0] == "我去订明天的会议室。" and stats(factory) == {"delegated": 1}


@pytest.mark.asyncio
async def test_a_check_that_writes_its_summary_loses_the_tags_and_remembers_them(agent):
    factory = agent["factory"]
    await turn(check(factory), scripted("(approval:ap_77)\n「发周报」等你批准已超过 3 小时。\n\n(task:pa_wi_12ab)\n「周报」失败了：邮箱授权过期。"))
    [(text, extra)] = replies(factory)
    assert [line for line in text.split("\n") if line] == ["「发周报」等你批准已超过 3 小时。", "「周报」失败了：邮箱授权过期。"]
    assert "(approval:" not in str(extra.get("segments")) and "(task:" not in str(extra.get("segments"))
    assert agent["told"] == [["approval:ap_77", "task:pa_wi_12ab"]] and stats(factory) == {"alert": 1}
    assert told(factory) == ["approval:ap_77", "task:pa_wi_12ab"]


@pytest.mark.asyncio
async def test_a_check_that_writes_only_tags_stays_quiet(agent):
    factory = agent["factory"]
    await turn(check(factory), scripted("(task:pa_wi_12ab)\n"))
    assert replies(factory) == [] and agent["told"] == [] and stats(factory) == {"quiet": 1}


def test_the_task_is_told_which_check_started_it():
    from personal_agent.tools.front_tools import proactive_note
    now = 1_800_000_000
    assert proactive_note({"window": "evening"}) == "This is the evening proactive check."
    idle = proactive_note({"window": "idle", "found": {"since": now - 2.5 * 3600}}, now)
    assert "new in the last 3 hours" in idle and "since_hours=3" in idle
    assert "since_hours=336" in proactive_note({"window": "audit", "found": {}})
    assert proactive_note({}) is None and proactive_note(None) is None


# ---------------------------------------------------------------- the whole chain, nothing replaced

@pytest.mark.asyncio
async def test_what_a_check_tells_is_under_already_told_at_the_next_check(agent):
    from personal_agent.conversation.router import context as front
    factory = agent["factory"]
    ref = "task:" + failed_task(factory, "生成本周销售周报", "邮箱授权过期")
    first = run_now(factory)
    context = front.turn_context(first)
    assert "Failed tasks:" in context and "(" + ref + ")" in context and "Do not raise these matters now:\n(none)" in context
    await turn(first, scripted("(" + ref + ")\n周报没有生成：邮箱授权过期了，重新授权后我再跑一次。"))
    [(text, extra)] = replies(factory)
    assert text == "周报没有生成：邮箱授权过期了，重新授权后我再跑一次。" and ref not in str(extra["segments"])
    assert told(factory) == [ref] and stats(factory) == {"alert": 1}
    context = front.turn_context(run_now(factory, later=120))
    block = context[context.index("# Already told"):context.index("# Your recent proactive messages")]
    assert "Do not raise these matters now:\n- " + ref + " (told " in block
    # The probe no longer names it, and the message itself is among the recent ones.
    assert "Failed tasks:" not in context and "周报没有生成" in context[context.index("# Your recent proactive messages"):]


@pytest.mark.asyncio
async def test_what_a_task_summary_tells_is_under_already_told_at_the_next_check(agent):
    from personal_agent.conversation.router import context as front
    factory = agent["factory"]
    failed = failed_task(factory, "生成本周销售周报", "邮箱授权过期")
    ref = "task:" + failed
    await turn(run_now(factory), scripted(("pa_task_create", {"goal": "邮箱：看有没有要回复的来信\n任务：周报失败 (" + ref + ")", "title": "主动服务"})))
    with factory() as db:
        task = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.id != failed)).one()
        assert task.goal.endswith(part("task_proactive") + "\n\nThe user started this proactive check by hand.")
        AgentContext(db, "owner").tasks.complete(task.id, "(" + ref + ")\n周报失败：邮箱授权过期。\n\n(email:4813)\n王总问报价，草稿在 /workspace/drafts/reply.md。", {})
        event = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).one().message_id
    assert "Do not raise these matters now:\n(none)" in front.turn_context(event)
    # The model writes one tag loosely, as it did in the live run; it is still removed and resolved.
    await turn(event, scripted("(ref: " + failed + ")\n周报没有生成：邮箱授权过期了。\n\n(email:4813)\n王总来信问报价，我起草了回复，你说一声我就发。"))
    [(text, extra)] = replies(factory)
    assert text == "周报没有生成：邮箱授权过期了。\n\n王总来信问报价，我起草了回复，你说一声我就发。"
    assert extra["heartbeat"] == "task" and failed not in str(extra.get("segments"))
    assert told(factory) == [ref, "email:4813"] and stats(factory) == {"delegated": 1, "reported": 1}
    context = front.turn_context(run_now(factory, later=120))
    block = context[context.index("# Already told"):context.index("# Your recent proactive messages")]
    assert "- " + ref + " (told " in block and "- email:4813 (told " in block
    assert "王总来信问报价" in context[context.index("# Your recent proactive messages"):]


@pytest.mark.asyncio
async def test_tags_leave_every_segment_of_a_summary_written_in_parts(agent):
    factory = agent["factory"]
    _, event = await task_result(factory, "(email:4813)\n王总问报价。\n\n(run:cr_77f0)\n导出失败。")

    async def new_input(plan):
        # As when the turn takes in input that arrived meanwhile: what was written stays, a new row follows.
        plan["reply"].boundary([])
        plan["reply"].take("model_call_start", {})
    await turn(event, scripted("(email:4813)\n王总来信问报价，我起草了回复。", new_input, "(run:cr_77f0)\n「客户名单导出」失败了。"))
    rows = replies(factory)
    assert [text for text, _ in rows] == ["王总来信问报价，我起草了回复。", "「客户名单导出」失败了。"]
    assert "(email:" not in str(rows) and "(run:" not in str(rows)
    assert told(factory) == ["run:cr_77f0", "email:4813"] or told(factory) == ["email:4813", "run:cr_77f0"]


# ---------------------------------------------------------------- the task's result

@pytest.mark.asyncio
async def test_a_proactive_result_gets_its_own_rules_and_a_scheduled_one_keeps_its(agent):
    from personal_agent.conversation.router import context as front
    factory = agent["factory"]
    heartbeat.remember_told(agent["agent_id"], ["email:4812"])
    _, event = await task_result(factory, "(email:4813)\n王总问报价，回复草稿在 /workspace/drafts/reply.md。")
    context = front.turn_context(event)
    assert "This turn: a background task finished" in context and part("front_event_proactive") in context
    assert "Already told" in context and "email:4812" in context and part("front_event_scheduled") not in context
    with factory() as db:
        svc = AgentContext(db, "owner")
        watch = svc.tasks.submit("检查价格是否低于 100", schedules.SCHEDULE_KEY_PREFIX + "s:1")["id"]
        svc.tasks.complete(watch, "价格仍为 120", {})
        scheduled = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending",
                                                                  PersonalMessageInbox.message_id != event)).one().message_id
    context = front.turn_context(scheduled)
    assert part("front_event_scheduled") in context and part("front_event_proactive") not in context and "Already told" not in context


@pytest.mark.asyncio
async def test_a_proactive_summary_is_sent_without_tags_and_counted_as_reported(agent):
    factory = agent["factory"]
    task_id, event = await task_result(factory, "(email:4813)\n王总问报价，草稿在 /workspace/drafts/reply.md。\n\n(news:https://example.com/pg17)\nPostgres 17.3 发布。")
    await turn(event, scripted("(email:4813)\n王总来信问报价，我起草了回复，你说一声我就发。\n\n(news:https://example.com/pg17)\n值得看的：Postgres 17.3 发布。https://example.com/pg17"))
    text, extra = replies(factory)[-1]
    assert text.startswith("王总来信问报价") and text.endswith("https://example.com/pg17")
    assert "(email:" not in text and "(news:" not in text and "(email:" not in str(extra.get("segments"))
    # It counts as a proactive message: later checks see it among the recent ones.
    assert extra["heartbeat"] == "task" and extra["work_item_id"] == task_id and not extra["streaming"]
    assert agent["told"] == [["email:4813", "news:https://example.com/pg17"]]
    assert told(factory) == ["email:4813", "news:https://example.com/pg17"]
    assert stats(factory) == {"delegated": 1, "reported": 1}
    with factory() as db: assert db.get(PersonalMessageInbox, event).decision["outcome"] == "reported"


@pytest.mark.asyncio
async def test_a_result_with_nothing_for_the_user_stays_quiet_and_counts_as_nothing(agent):
    factory = agent["factory"]
    _, event = await task_result(factory, "(email:4812)\n王总的信还没回。")
    await turn(event, scripted(("pa_stay_quiet", {"reason": "已经告诉过用户"})))
    assert replies(factory) == [] and agent["told"] == []
    assert stats(factory) == {"delegated": 1, "nothing": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("written", ["failed", "only tags", "nothing"])
async def test_a_summary_that_was_not_written_sends_nothing_and_never_the_report(agent, written):
    """Review S6: the task's report is for the agent. It may carry links from mail, paths and ids."""
    factory = agent["factory"]
    _, event = await task_result(factory, "(email:4813)\n王总问报价，草稿在 /workspace/drafts/reply.md。原文链接 https://evil.example/x")

    async def broken(plan, binding): raise RuntimeError("model offline")
    await turn(event, broken if written == "failed" else scripted("(email:4813)\n" if written == "only tags" else "HEARTBEAT_OK"))
    assert replies(factory) == [] and agent["told"] == [] and told(factory) == [] and agent["noted"] == []
    assert stats(factory) == {"delegated": 1, "task_failed": 1}
    with factory() as db:
        row = db.get(PersonalMessageInbox, event)
        # Not left as failed: nothing for the user to process again.
        assert row.status == "done" and row.decision["quiet"] and row.decision["outcome"] == "task_failed"


@pytest.mark.asyncio
async def test_only_the_first_summary_carries_the_note(agent):
    factory = agent["factory"]
    first_summary_pending(factory)
    _, event = await task_result(factory, "(email:4813)\n王总问报价。")
    await turn(event, scripted("(email:4813)\n王总来信问报价，我起草了回复。"))
    await turn(check(factory), scripted("(approval:ap_77)\n「发周报」等你批准已超过 3 小时。"))
    first, second = (text for text, _ in replies(factory))
    note = agent["noted"][0]
    assert note and note.startswith(heartbeat.FIRST_NOTE) and agent["noted"] == [note, None]
    assert first == "王总来信问报价，我起草了回复。\n\n" + note and second == "「发周报」等你批准已超过 3 小时。"


@pytest.mark.asyncio
async def test_a_task_the_user_took_over_reports_like_any_other(agent):
    from personal_agent.conversation.router import context as front
    factory = agent["factory"]
    await turn(check(factory), scripted(("pa_task_create", {"goal": "检查邮箱", "title": "主动服务"})))
    with factory() as db:
        task = db.scalars(select(PersonalWorkItem)).one()
        # The user continued it ("发吧"): it is their task now. The worker's checkpoint carries the mark.
        AgentContext(db, "owner").tasks.complete(task.id, "(email:4813)\n已发送给王总。", {"continuation_seq": 5})
        event = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).one().message_id
    assert part("front_event_proactive") not in front.turn_context(event)
    await turn(event, scripted("给王总的回复发出去了。"))
    text, extra = replies(factory)[-1]
    assert text == "给王总的回复发出去了。" and "heartbeat" not in extra
    assert agent["told"] == [] and stats(factory) == {"delegated": 1}


def test_tags_are_removed_and_resolved_however_the_model_writes_them():
    """Found in the live run: the model wrote "(ref: <chat id>)", which reached the user and was not stored."""
    from personal_agent.scheduling import heartbeat
    text, refs = heartbeat.take_refs("(ref: chat_b)\n合同比对卡住了。\n\n（run:run_x1）\n第二件。\n\n(注意:这不是标记)\n行内的 (email:12) 不动。")
    assert text == "合同比对卡住了。\n\n第二件。\n\n(注意:这不是标记)\n行内的 (email:12) 不动。"
    assert refs == ["chat_b", "run:run_x1"]
    leads = {"chat:chat_b:1794299984": 1.0, "run:run_x1": 1.0}
    assert heartbeat.resolve_refs(refs + ["chat:chat_b", "email:7", "pa_unknown"], leads) == ["chat:chat_b:1794299984", "run:run_x1", "email:7"]


def test_a_matter_named_by_its_title_is_told_without_a_tag():
    """Found in the live run: the model wrote a clean summary with no tags, and nothing was stored."""
    from personal_agent.scheduling import heartbeat
    names = {"run:run_d": "[E2E] 客户名单导出", "chat:chat_c:179": "[E2E] 季度销售复盘", "chat:chat_x:1": "没提到的对话"}
    text = "「[E2E] 客户名单导出」失败的原因查到了。另外「[E2E] 季度销售复盘」还在等你回。"
    assert heartbeat.named_refs(text, names) == ["run:run_d", "chat:chat_c:179"]
    assert heartbeat.named_refs("", names) == [] and heartbeat.named_refs(text, None) == []


# ---------------------------------------------------------------- nobody is there: what such a turn may do

def tool_names(plan_or_ident):
    return set(tools_for(plan_or_ident))


@pytest.mark.asyncio
async def test_proactive_turns_cannot_write_memory_persona_or_the_list(agent):
    """Review S4: a check reads the internet and a result turn reads the task's report; neither is the user's word."""
    factory, seen = agent["factory"], {}
    writes = {"pa_memory_edit", "pa_memory_write", "pa_set_name", "pa_proactive_dismiss"}

    def look(name):
        async def step(plan): seen[name] = tool_names(plan["turn"].id)
        return step
    await turn(check(factory), scripted(look("check"), ("pa_task_create", {"goal": "检查邮箱", "title": "主动服务"})))
    with factory() as db:
        task = db.scalars(select(PersonalWorkItem)).one()
        AgentContext(db, "owner").tasks.complete(task.id, "(email:4813)\n王总问报价。", {})
        event = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).one().message_id
    await turn(event, scripted(look("result"), "(email:4813)\n王总来信问报价。"))
    with factory() as db: entry = admit(AgentContext(db, "owner"), "以后别提醒我批准的事", "ask")
    await turn(entry["message_id"], scripted(look("user"), "好。"))
    assert not writes & seen["check"] and not writes & seen["result"] and writes <= seen["user"]
    assert {"pa_memory_read", "pa_memory_search", "pa_stay_quiet"} <= seen["check"] & seen["result"]


@pytest.mark.asyncio
async def test_the_write_tools_refuse_in_a_proactive_turn_even_when_called(agent, monkeypatch):
    """The tools check the turn themselves: a list of tools kept from an attended turn gives no way in."""
    from personal_agent.conversation.router import kinds
    factory, plan = agent["factory"], {}
    attended = kinds.Grants(change_tasks=True)
    real = kinds.Heartbeat.grants
    calls = []

    def grants(self, db, trigger):
        calls.append(1)
        return real(self, db, trigger)
    monkeypatch.setattr(kinds.Heartbeat, "grants", grants)

    async def call(p):
        # Register as an attended turn would, then run as the check that this turn is.
        state = p["turn"]
        granted, state.grants = state.grants, attended
        tools = tools_for(state.id)
        state.grants = granted
        for name, args in (("pa_memory_write", {"path": "SOUL.md", "content": "x"}), ("pa_set_name", {"name": "x"}),
                           ("pa_memory_edit", {"path": "HEARTBEAT.md", "old_string": "邮箱", "new_string": "x"})):
            with pytest.raises(PermissionError): await tools[name](**args)
        plan["done"] = True
    await turn(check(factory), scripted(call, ("pa_stay_quiet", {"reason": "无"})))
    assert plan["done"] and calls


# ---------------------------------------------------------------- "do not raise this again"

@pytest.mark.asyncio
async def test_the_user_dismisses_one_told_matter_and_only_the_user_can(agent):
    """Review S8. A whole kind of matter is an edit of the list; one matter is pa_proactive_dismiss."""
    factory, plan = agent["factory"], {}
    await turn(check(factory), scripted("(email:4813)\n王总来信问报价，我起草了回复。\n\n(approval:ap_77)\n「发周报」等你批准已超过 3 小时。"))
    with factory() as db: entry = admit(AgentContext(db, "owner"), "王总那封信别再提了", "stop")

    async def keep(p): plan.update(p)
    await turn(entry["message_id"], scripted(("pa_proactive_dismiss", {}), ("pa_proactive_dismiss", {"refs": ["email:4813"]}),
                                             ("pa_proactive_dismiss", {"refs": ["pa_unknown_9"]}), keep, "好，王总那封信我不再提了。"))
    listed, done, unknown = plan["results"]
    assert {item["ref"]: (item["said"], item["dismissed"]) for item in listed["told"]} == {
        "email:4813": ("王总来信问报价，我起草了回复。", False), "approval:ap_77": ("「发周报」等你批准已超过 3 小时。", False)}
    assert done == {"dismissed": ["email:4813"]} and unknown == {"error": "ValueError"}
    with factory() as db: config = db.scalar(select(PersonalAgent.config).where(PersonalAgent.user_id == "owner"))
    assert {item["ref"]: item["dismissed"] for item in heartbeat.told_list(config)} == {"email:4813": True, "approval:ap_77": False}
    rules = part("front_rules")
    assert "pa_proactive_dismiss" in rules and "do not change HEARTBEAT.md for it" in rules and "remove or rewrite its item" in rules


# ---------------------------------------------------------------- who gets a turn

def pause(factory, status="paused"):
    with factory() as db:
        db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == "owner")).status = status
        db.commit()


def inbox(factory, ident):
    with factory() as db:
        row = db.get(PersonalMessageInbox, ident)
        return row.status, dict(row.decision or {})


@pytest.mark.asyncio
async def test_a_paused_agent_answers_its_user_but_starts_nothing_on_its_own(agent):
    """Review S10: a check admitted before the pause made a model call and could send its summary."""
    factory = agent["factory"]
    _, result = await task_result(factory, "(email:4813)\n王总问报价。")
    waiting = check(factory)
    # Sent just before the pause; a paused agent admits no new message, but this one is answered.
    with factory() as db: entry = admit(AgentContext(db, "owner"), "你还在吗", "hi")
    pause(factory)
    turn_id, claimed = claim("w")
    # The check is dropped (its window stays open); the result waits for the agent to be resumed.
    assert inbox(factory, waiting)[1]["outcome"] == "stale" and inbox(factory, result)[0] == "pending"
    if turn_id is None: turn_id, claimed = claim("w")
    assert claimed == [entry["message_id"]]
    end(factory, turn_id)
    assert claim("w") == (None, [])
    pause(factory, "ready")
    assert claim("w")[1] == [result]


@pytest.mark.asyncio
async def test_a_summary_that_waited_for_hours_is_not_sent(agent):
    from datetime import datetime, timedelta, timezone
    from personal_agent.conversation.router import scheduler
    factory = agent["factory"]
    _, result = await task_result(factory, "(email:4813)\n王总问报价。")
    with factory() as db:
        db.get(PersonalMessageInbox, result).created_at = datetime.now(timezone.utc) - timedelta(seconds=scheduler.PROACTIVE_RESULT_MAX_SEC + 60)
        db.commit()
    assert claim("w") == (None, [])
    status, decision = inbox(factory, result)
    assert status == "done" and decision["quiet"] and decision["outcome"] == "stale"
    assert replies(factory) == [] and stats(factory) == {"delegated": 1, "task_failed": 1}
    # A result the user asked for never expires.
    with factory() as db:
        svc = AgentContext(db, "owner"); asked = svc.tasks.submit("查快递", "asked")["id"]; svc.tasks.complete(asked, "已签收", {})
        event = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).one()
        event.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        ident = event.message_id
        db.commit()
    assert claim("w")[1] == [ident]


@pytest.mark.asyncio
async def test_proactive_turns_come_last_and_leave_room_for_the_users_messages(agent):
    """Review C4: checks and their summaries share the Router's parallel turns with what users send."""
    from personal_agent.conversation.router import scheduler
    factory = agent["factory"]
    _, result = await task_result(factory, "(email:4813)\n王总问报价。")
    waiting = check(factory)
    # A worker that runs its share of proactive turns starts no more of them.
    assert scheduler.claim_next("w", user_only=True) == (None, False)
    assert inbox(factory, result)[0] == "pending" and inbox(factory, waiting)[0] == "pending"
    with factory() as db: entry = admit(AgentContext(db, "owner"), "帮我看下明天的日程", "ask")
    turn_id, proactive = scheduler.claim_next("w", user_only=True)
    with factory() as db:
        claimed = [row.message_id for row in db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.turn_id == turn_id))]
    # The user's message goes first, although the result and the check were there before it, and it takes neither along.
    assert claimed == [entry["message_id"]] and proactive is False
    end(factory, turn_id)
    turn_id, proactive = scheduler.claim_next("w")
    assert proactive is True and inbox(factory, result)[0] == "claimed" and inbox(factory, waiting)[0] == "pending"
    end(factory, turn_id)
    assert scheduler.claim_next("w")[1] is True and inbox(factory, waiting)[0] == "claimed"
    assert 0 < scheduler.PROACTIVE_OWNERS < scheduler.PARALLEL_OWNERS


@pytest.mark.asyncio
async def test_the_first_note_is_not_spent_by_a_settlement_that_fails(agent, monkeypatch):
    """Review C10: the note was taken, and committed, before the rest of the settlement could still fail."""
    from personal_agent.conversation.router import kinds
    factory = agent["factory"]
    first_summary_pending(factory)
    _, event = await task_result(factory, "(email:4813)\n王总问报价。")
    deliver = kinds.TaskResult.deliver

    def broken(self, db, trigger, reply): raise RuntimeError("delivery could not be queued")
    monkeypatch.setattr(kinds.TaskResult, "deliver", broken)
    await turn(event, scripted("(email:4813)\n王总来信问报价。"))
    assert agent["noted"] == [] and replies(factory) == []
    monkeypatch.setattr(kinds.TaskResult, "deliver", deliver)
    await turn(check(factory), scripted("(approval:ap_77)\n「发周报」等你批准已超过 3 小时。"))
    [(text, extra)] = replies(factory)
    assert text == "「发周报」等你批准已超过 3 小时。\n\n" + agent["noted"][0] and agent["noted"][0].startswith(heartbeat.FIRST_NOTE)
    assert heartbeat.FIRST_NOTE in str(extra.get("segments"))


# ---------------------------------------------------------------- the task's report is never the message

@pytest.mark.asyncio
async def test_a_long_report_still_goes_through_the_agent(agent):
    """A long result is posted as it is, for a task the user asked for. A proactive task's report is written
    for the agent (tags, paths, what mail and pages said): however long, the agent writes the summary."""
    from personal_agent.conversation.router.triggers import CUT, LONG_RESULT
    factory = agent["factory"]
    report = "(email:4813)\n王总问报价，草稿在 /workspace/drafts/reply.md。\n" + "邮件原文。" * (LONG_RESULT // 5 + 10)
    task_id, event = await task_result(factory, report)
    assert replies(factory) == []
    with factory() as db:
        shown = db.get(ChatMessage, event).content
        assert len(report) > LONG_RESULT and shown == report[:LONG_RESULT] + "\n" + CUT
        # The whole report stays on the task.
        assert db.get(PersonalWorkItem, task_id).result == report
    await turn(event, scripted("(email:4813)\n王总来信问报价，我起草了回复。"))
    assert [text for text, _ in replies(factory)] == ["王总来信问报价，我起草了回复。"] and told(factory) == ["email:4813"]


# ---------------------------------------------------------------- a restore is always told

@pytest.mark.asyncio
@pytest.mark.parametrize("written", ["left out", "kept", "quiet", "failed"])
async def test_the_user_always_hears_that_settings_were_put_back(agent, written):
    from personal_agent.scheduling.proactive_guard import NOTE, RESTORED
    factory = agent["factory"]
    await turn(check(factory), scripted(("pa_task_create", {"goal": "检查邮箱", "title": "主动服务"})))
    with factory() as db:
        task = db.scalars(select(PersonalWorkItem)).one()
        AgentContext(db, "owner").tasks.complete(task.id, "(email:4813)\n王总问报价。\n\n" + NOTE, {RESTORED: ["/workspace/SOUL.md"]})
        event = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).one().message_id

    async def broken(plan, binding): raise RuntimeError("model offline")
    summary = "(email:4813)\n王总来信问报价，我起草了回复。"
    await turn(event, {"left out": scripted(summary), "kept": scripted(summary + "\n\n" + NOTE),
                       "quiet": scripted("先看看。", ("pa_stay_quiet", {"reason": "已经告诉过用户"})), "failed": broken}[written])
    [(text, extra)] = replies(factory)
    # The sentence is the platform's: once, at the end, and alone when the turn has nothing else to send.
    assert text == ("王总来信问报价，我起草了回复。\n\n" + NOTE if written in ("left out", "kept") else NOTE)
    assert str(extra["segments"]).count(NOTE) == 1 and "先看看" not in str(extra["segments"]) and extra["heartbeat"] == "task"
    assert stats(factory) == {"delegated": 1, {"quiet": "nothing", "failed": "task_failed"}.get(written, "reported"): 1}
    assert told(factory) == (["email:4813"] if written in ("left out", "kept") else [])


@pytest.mark.asyncio
async def test_a_summary_without_a_restore_says_nothing_about_one(agent):
    from personal_agent.scheduling.proactive_guard import NOTE
    factory = agent["factory"]
    _, event = await task_result(factory, "(email:4813)\n王总问报价。")
    await turn(event, scripted("(email:4813)\n王总来信问报价。"))
    assert NOTE not in replies(factory)[-1][0]


# ---------------------------------------------------------------- the user answers a summary

@pytest.mark.asyncio
async def test_a_reply_to_a_summary_reaches_the_agent_with_its_task_and_changes_nothing_by_itself(agent):
    """A quoted reply to a proactive summary is an ordinary message (channels/im_actions.route): "先别发"
    must not continue the task. The agent sees which task it is about and decides."""
    from personal_agent.conversation.router import context as front
    factory = agent["factory"]
    task_id, event = await task_result(factory, "(email:4813)\n王总问报价，草稿在 /workspace/drafts/reply.md。")
    await turn(event, scripted("(email:4813)\n王总来信问报价，我起草了回复，你说一声我就发。"))
    with factory() as db:
        summary = db.scalars(select(ChatMessage).where(ChatMessage.role == "assistant")).one().message_id
        quoted = admit(AgentContext(db, "owner"), "先别发，谢谢", "quoted", reply_to=summary)["message_id"]
        context = front.turn_context(quoted)
        referenced = context[context.index("Tasks selected or referenced by this message"):]
        assert "a reference does not by itself request a change" in referenced and task_id in referenced
        # The history holds the task's report and the reply. (The summary itself is replayed from the model's
        # own steps, which the scripted model of these tests does not produce.)
        last = front.history(db, quoted)[-1]["content"]
        assert "[Task result:" in last and "王总问报价" in last and last.endswith("先别发，谢谢")
    plan = {}

    async def keep(p): plan.update(p)
    # The task is not continued by the reply itself, and cannot be without reading it first.
    await turn(quoted, scripted(("pa_task_update", {"task_id": task_id, "instruction": "发送"}), keep, "好，先不发。"))
    assert plan["results"] == [{"error": "ValueError"}]
    with factory() as db:
        task = db.get(PersonalWorkItem, task_id)
        assert task.status == "completed" and schedules.is_proactive(task)
    rules = part("front_rules")
    assert "only when they clearly ask you to go on or to send" in rules and "changes no task" in rules


# ---------------------------------------------------------------- what the adversarial end-to-end run found

@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["quoted", "latest", "old", "answered since"])
async def test_a_task_started_in_answer_to_a_summary_is_marked_by_the_platform(agent, how):
    """E2E 1: "按草稿发吧" became an ordinary new task and the mail went out unapproved. Whether a new task
    answers a proactive summary is decided by code: the user quotes the summary, or it is the agent's
    latest message and at most a day old. The mark makes its outward actions need approval one by one."""
    from datetime import timedelta
    from core.infra.time import utc_now
    from personal_agent.scheduling import proactive_tasks
    from personal_agent.scheduling.proactive_tasks import FOLLOWUP
    factory = agent["factory"]
    _, event = await task_result(factory, "(email:4813)\n王总问报价，草稿在 /workspace/drafts/reply.md。")
    await turn(event, scripted("(email:4813)\n王总来信问报价，我起草了回复。"))
    with factory() as db:
        summary = db.scalars(select(ChatMessage).where(ChatMessage.role == "assistant")).one()
        # A quoted summary counts however old it is; an unquoted one only for a day.
        if how in ("quoted", "old"): summary.created_at = utc_now() - timedelta(hours=30)
        ident = summary.message_id
        db.commit()
    if how == "answered since":
        with factory() as db: first = admit(AgentContext(db, "owner"), "知道了", "ack")["message_id"]
        await turn(first, scripted("好。"))
    with factory() as db:
        sent = admit(AgentContext(db, "owner"), "按草稿发吧", "send", reply_to=ident if how == "quoted" else None)["message_id"]
    await turn(sent, scripted(("pa_task_create", {"goal": "把草稿发给王总", "title": "发信"}), "我去发，发之前会请你批准。"))
    with factory() as db:
        tasks = db.scalars(select(PersonalWorkItem).order_by(PersonalWorkItem.created_at)).all()
        marks = {("check" if heartbeat.is_beat_key(task.request_key) else "user"): bool((task.checkpoint or {}).get(FOLLOWUP)) for task in tasks}
        began = [proactive_tasks.began(task) for task in tasks]
    # The check's own task is known by its key; the mark is only for what the user started after it.
    assert marks == {"check": False, "user": how in ("quoted", "latest")}
    # Either way its outward actions are approved one by one.
    assert began == [True, how in ("quoted", "latest")]


@pytest.mark.asyncio
async def test_a_task_that_uses_a_prepared_draft_is_marked_whenever_it_starts(agent):
    """Days later and without quoting: a goal that names the drafts folder acts on what a check prepared."""
    from personal_agent.scheduling import proactive_tasks
    factory = agent["factory"]
    with factory() as db: sent = admit(AgentContext(db, "owner"), "把上次给王总的草稿发了", "send")["message_id"]
    await turn(sent, scripted(("pa_task_create", {"goal": "发送 /workspace/drafts/reply.md 给王总", "title": "发信"}), "我去发。"))
    with factory() as db: assert proactive_tasks.began(db.scalars(select(PersonalWorkItem)).one())


def test_claiming_a_turn_never_waits_for_a_running_one(agent):
    """E2E 6, a PostgreSQL deadlock: the claim held the owner's row FOR UPDATE and waited for the Turn row,
    while the running turn held its Turn row and added a task, whose foreign key needs the owner's row.
    Now the owner's lock lets key-share locks pass, and a held Turn row is skipped, not waited for."""
    from sqlalchemy.dialects import postgresql
    from personal_agent.conversation.router import scheduler
    from personal_agent.models import PersonalFrontTurn

    def sql(query): return " ".join(str(query.compile(dialect=postgresql.dialect())).split())
    assert sql(scheduler.owner_lock("a")).endswith("FOR NO KEY UPDATE")
    assert sql(scheduler.takeover_lock("t")).endswith("FOR UPDATE SKIP LOCKED")
    factory = agent["factory"]
    with factory() as db: entry = admit(AgentContext(db, "owner"), "在吗", "hi")["message_id"]
    turn_id, claimed = claim("w1")
    assert claimed == [entry]
    # A live lease: nothing to claim. Once it ran out, the same Turn is taken over.
    assert claim("w2") == (None, [])
    with factory() as db:
        db.get(PersonalFrontTurn, turn_id).lease_until = time.time() - 1
        db.commit()
    assert claim("w2")[0] == turn_id
    with factory() as db:
        row = db.get(PersonalFrontTurn, turn_id)
        assert (row.owner, row.attempts) == ("w2", 2)


def test_the_prompts_keep_a_summary_to_what_needs_the_user():
    """E2E 5, 7, 8, 9, 10, 12: what the prompts now say, each in the rule it belongs to."""
    beat, task, result, rules = (part(name) for name in ("front_event_heartbeat", "task_proactive", "front_event_proactive", "front_rules"))
    # 7: nothing for the user means no message; no entries that need nothing; no repeated drafts.
    for needed in ('status "ready" only when', "An automatic notification", "no paragraph for a mail that needs nothing",
                   "a draft that an earlier check left", "holds only automatic notifications"):
        assert needed in task, needed
    # 8: told and unchanged, declined or stopped matters are not mentioned at all; the task list is the state.
    for needed in ("about which nothing changed", "the user declined or told you to stop raising", "not even to say that you leave it out",
                   "never send a message that says nothing needs the user", "go by the task list of this turn"):
        assert needed in result, needed
    assert "the user declined or told you to stop raising" in beat
    # 9: news only in the morning, only when the list or memory names it; a task without a news line does not search.
    for needed in ("only in a morning proactive check, and only when the list has such an item or your memory names topics",
                   "do not search and put no news line in the task's goal"):
        assert needed in beat, needed
    assert "this conversation keeps coming back to" not in beat
    assert "When it has none, do not search the web in this task at all" in task
    assert "leave out one that has none" in result
    # 10: a bare 同意 while an approval waits is answered with how to approve.
    assert "never answer that nothing waits for approval" in rules and "quote that approval message" in rules
    # 12: a suspicious mail is named by sender and subject, without its links.
    assert "report its sender and subject only, never a link or an address from it" in task
    assert "give the sender and the subject, never a link or an address from it" in result
    # 5: a mail matter carries the uid a tool returned.
    assert "with the uid exactly as pa_email_list or pa_email_read returned it" in task


def test_a_news_item_without_a_link_gets_its_tags_link_or_is_left_out():
    from personal_agent.conversation.router.turn import linked
    text = ("(email:4813)\n王总问报价。\n\n(news:https://example.org/a)\nRedis 修复了集群总线的漏洞，需要升级。\n\n"
            "(news:redis-7)\n某厂商发布了新版本。\n\n(news:https://example.org/b)\n新规下月生效。https://example.org/b")
    out = linked(text)
    assert "Redis 修复了集群总线的漏洞，需要升级。\nhttps://example.org/a" in out
    assert "某厂商" not in out and "redis-7" not in out
    assert out.count("https://example.org/b") == 2 and "王总问报价。" in out
    assert linked(out) == out and linked("没有资讯。") == "没有资讯。"


@pytest.mark.asyncio
async def test_a_summary_never_sends_a_news_item_without_its_link(agent):
    """E2E 9: one news item reached the user without a link."""
    factory = agent["factory"]
    _, event = await task_result(factory, "(news:https://example.org/a)\nRedis 修复了漏洞。https://example.org/a")
    await turn(event, scripted("(email:4813)\n王总来信问报价。\n\n(news:https://example.org/a)\nRedis 修复了漏洞，需要升级。\n\n"
                               "(news:redis-7)\n某厂商发布了新版本。"))
    [(text, extra)] = replies(factory)
    assert text == "王总来信问报价。\n\nRedis 修复了漏洞，需要升级。\nhttps://example.org/a"
    assert "某厂商" not in str(extra["segments"]) and "https://example.org/a" in str(extra["segments"])
    assert not any("redis-7" in ref for ref in told(factory)) and "email:4813" in told(factory)


# ---------------------------------------------------------------- news is sent once

NEWS = "Redis 修复了集群总线的漏洞，需要升级。https://example.org/advisory/123"


async def summary_after(factory, written, restored=False):
    """One more proactive task reports and its result turn writes `written`."""
    from personal_agent.scheduling.proactive_guard import RESTORED
    await turn(check(factory), scripted(("pa_task_create", {"goal": "检查邮箱和资讯", "title": "主动服务"})))
    with factory() as db:
        task = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.status != "completed")).one()
        AgentContext(db, "owner").tasks.complete(task.id, "报告", {RESTORED: ["/workspace/SOUL.md"]} if restored else {})
        event = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.status == "pending")).one().message_id
    await turn(event, scripted(written))


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["with a matter", "news only", "news only, restored"])
async def test_a_news_item_is_sent_once(agent, second):
    """E2E 9: the same address was reported twice within 20 minutes. A summary loses the news items whose
    addresses were all told; what else it says stays, and with nothing else it is not sent."""
    from personal_agent.scheduling.proactive_guard import NOTE
    factory = agent["factory"]
    await summary_after(factory, "(news:https://example.org/advisory/123)\n" + NEWS)
    assert [text for text, _ in replies(factory)] == [NEWS] and told(factory) == ["news:https://example.org/advisory/123"]
    # The same page comes back with another spelling of its address.
    again = "(news:https://example.org/advisory/123/)\nRedis 的漏洞修复了。https://EXAMPLE.org/advisory/123/?utm_source=x"
    await summary_after(factory, ("(email:4813)\n王总来信问报价，我起草了回复。\n\n" if second == "with a matter" else "") + again,
                        restored=second.endswith("restored"))
    sent = replies(factory)[1:]
    if second == "with a matter":
        [(text, extra)] = sent
        assert text == "王总来信问报价，我起草了回复。" and "example" not in str(extra["segments"]).lower()
        assert stats(factory)["reported"] == 2
    elif second == "news only":
        assert sent == [] and stats(factory) == {"delegated": 2, "reported": 1, "nothing": 1}
    else:
        # A restore is still told, alone.
        assert [text for text, _ in sent] == [NOTE] and stats(factory)["nothing"] == 1
    with factory() as db: config = db.scalar(select(PersonalAgent.config).where(PersonalAgent.user_id == "owner"))
    news = [item for item in config["heartbeat"]["told"] if item["ref"].startswith("news:")]
    # Not told a second time: the entry is the one of the first summary.
    assert len(news) == 1 and news[0].get("times", 1) == 1
    assert ("email:4813" in told(factory)) == (second == "with a matter")


# ---------------------------------------------------------------- the second adversarial run

def set_links(factory, value):
    """What the admission of the latest check wrote: whether it looks for news."""
    with factory() as db:
        update_config(db, "owner", lambda config: config.update(heartbeat={**(config.get("heartbeat") or {}),
            "proactive": {**((config.get("heartbeat") or {}).get("proactive") or {}), "links": value}}))


@pytest.mark.asyncio
async def test_a_check_that_started_its_task_sends_nothing_itself(agent):
    """Retest 1: one check sent two messages, its own news and then its task's summary."""
    factory = agent["factory"]
    await turn(check(factory), scripted(("pa_task_create", {"goal": "检查邮箱", "title": "主动服务"}),
                                        "先说一条资讯：Redis 修复了漏洞。https://example.org/advisory/123"))
    assert replies(factory) == [] and stats(factory) == {"delegated": 1} and told(factory) == []
    assert "anything you write is discarded" in part("front_event_heartbeat")


def test_the_result_turn_is_told_last_what_a_paragraph_must_hold(agent):
    """Retest 2: three of five summaries said "这些通知不用处理"."""
    from personal_agent.conversation.router import context as front
    from personal_agent.conversation.router.kinds import NO_NEWS
    factory = agent["factory"]
    import asyncio
    _, event = asyncio.run(task_result(factory, "(email:4813)\n王总问报价。"))
    context = front.turn_context(event)
    last = part("front_event_proactive_last")
    # After the told matters, at the very end. The check was a morning one of unknown news state: no news line.
    assert context.index("# Already told") < context.index(last) and context.rstrip().endswith(last + "\n</turn_context>")
    for needed in ("one concrete thing that the user must decide, answer or know now", "Automatic notifications, subscriptions and system mail",
                   "这些通知不用处理", "没有需要回复的新邮件", "pa_stay_quiet"):
        assert needed in last, needed
    assert "an automatic notification, a subscription or a system mail" in part("front_event_proactive")
    task = part("task_proactive")
    assert "do not retell it to say that it needs nothing" in task and "这些通知不用处理" in task and "没有需要回复的新邮件" in task
    set_links(factory, False)
    assert front.turn_context(event).rstrip().endswith(last + "\n" + NO_NEWS + "\n</turn_context>")


def test_a_check_is_told_last_when_it_looks_for_no_news(agent):
    """Retest 3: a check of a list without a news item, not in the morning, still reported news."""
    from personal_agent.conversation.router import context as front
    from personal_agent.conversation.router.kinds import NO_NEWS
    factory = agent["factory"]
    morning, manual = check(factory, "morning"), check(factory, "manual")
    assert NO_NEWS not in front.turn_context(morning) and front.turn_context(manual).rstrip().endswith(NO_NEWS + "\n</turn_context>")
    set_links(factory, False)
    assert front.turn_context(morning).rstrip().endswith(NO_NEWS + "\n</turn_context>")
    set_links(factory, True)
    assert NO_NEWS not in front.turn_context(morning) and NO_NEWS in front.turn_context(manual)


@pytest.mark.asyncio
@pytest.mark.parametrize("written", ["with a matter", "news only"])
async def test_a_check_that_looks_for_no_news_sends_none(agent, written):
    """Retest 3, by code: when the admission said no news, a paragraph with a web address that belongs to no
    tagged matter is not sent. A matter's own link stays."""
    factory = agent["factory"]
    _, event = await task_result(factory, "报告")
    set_links(factory, False)
    news = "资讯：\n\nPostgreSQL 14 发布了安全更新。https://example.org/pg14\n\n(news:https://example.org/b)\n新规下月生效。https://example.org/b"
    matter = "(email:4813)\n王总来信问报价，报价单在 https://corp.example/quote/7。"
    await turn(event, scripted((matter + "\n\n" if written == "with a matter" else "") + news))
    if written == "with a matter":
        [(text, extra)] = replies(factory)
        assert text == "王总来信问报价，报价单在 https://corp.example/quote/7。"
        assert "example.org" not in str(extra["segments"]) and "资讯" not in str(extra["segments"])
        assert told(factory) == ["email:4813"]
    else:
        assert replies(factory) == [] and stats(factory) == {"delegated": 1, "nothing": 1} and told(factory) == []


def test_only_untagged_paragraphs_with_addresses_read_as_news():
    from personal_agent.conversation.router.turn import unlinked
    text = ("(run:cr_77f0)\n运行失败，日志在 https://corp.example/runs/7。\n\n周报还没交。\n\n今日资讯：\n\n- A 发布。https://example.org/a\n\n"
            "(news:https://example.org/b)\nB 生效。")
    assert unlinked(text) == "(run:cr_77f0)\n运行失败，日志在 https://corp.example/runs/7。\n\n周报还没交。"
    assert unlinked("没有网址。") == "没有网址。"


ORIGIN = {"channel_id": "bot", "channel_type": "lark", "owner": True,
          "msg": {"external_conversation_id": "alice", "chat_type": "p2p", "sender_id": "alice"}}


async def summary_sent_to_im(factory):
    """A proactive summary that went to the owner's direct IM conversation. (the IM conversation, the task, the summary)."""
    from tests.test_personal_agent_channel_sessions import conversation
    with factory() as db: chat_id = conversation(db)
    task_id, event = await task_result(factory, "(email:4813)\n王总问报价，草稿在 /workspace/drafts/reply.md。")
    await turn(event, scripted("(email:4813)\n王总来信问报价，我起草了回复，你说一声我就发。"))
    with factory() as db:
        summary = db.scalars(select(ChatMessage).where(ChatMessage.role == "assistant")).one()
        summary.extra_data = {**summary.extra_data, "channel_delivery": {"origin": ORIGIN, "status": "sent", "message_id": "im_1"}}
        db.commit()
        return chat_id, task_id, summary.message_id


@pytest.mark.asyncio
async def test_an_unquoted_im_reply_sees_the_summary_and_what_waits_for_approval(agent):
    """Retest 4: in the IM conversation the agent saw neither the summary nor the approval request, both rows
    of the main conversation: "发吧" got "我这里没有待发送的内容", "同意" no hint to quote the request."""
    from personal_agent.conversation.router import context as front
    from personal_agent.scheduling import proactive_tasks
    factory = agent["factory"]
    chat_id, task_id, _ = await summary_sent_to_im(factory)
    with factory() as db:
        web = admit(AgentContext(db, "owner"), "今天有什么安排", "web")["message_id"]
        assert "Sent to this IM conversation lately" not in front.turn_context(web)
    await turn(web, scripted("今天没有安排。"))
    with factory() as db:
        # A second task waits for approval, asked in the same IM conversation.
        waiting = db.get(PersonalWorkItem, task_id)
        waiting.status = "waiting"
        waiting.checkpoint = {**(waiting.checkpoint or {}), "pending_approval": {"id": "ap_1", "action": {}},
            "approval_notice": {"approval_id": "ap_1", "channel_id": "bot", "peer": "alice", "chat_type": "p2p", "message_id": "m"}}
        db.commit()
        sent = admit(AgentContext(db, "owner", chat_id=chat_id), "同意，发吧", "im", extra={"channel_origin": ORIGIN})["message_id"]
        context = front.turn_context(sent)
    block = context[context.index("# Sent to this IM conversation lately"):]
    assert "> 王总来信问报价，我起草了回复，你说一声我就发。" in block and "email:4813" in block
    assert "wait for the user's approval" in block and "quote that approval message" in block and "or to approve on the web page" in block
    referenced = context[context.index("Tasks selected or referenced by this message"):]
    assert task_id in referenced
    # A task the user starts from here is a follow-up of the summary, as in the main conversation.
    await turn(sent, scripted(("pa_task_create", {"goal": "把回复发给王总", "title": "发信"}), "请引用那条批准消息回复同意。"))
    with factory() as db:
        new = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.id != task_id)).one()
        assert (new.checkpoint or {}).get(proactive_tasks.FOLLOWUP) is True
        # Once the agent has answered in this IM conversation, a later message no longer answers the summary.
        later = admit(AgentContext(db, "owner", chat_id=chat_id), "再帮我订个会议室", "im2", extra={"channel_origin": ORIGIN})["message_id"]
    await turn(later, scripted(("pa_task_create", {"goal": "订明天的会议室", "title": "订会议室"}), "我去订。"))
    with factory() as db:
        last = db.scalars(select(PersonalWorkItem).order_by(PersonalWorkItem.created_at.desc())).first()
        assert last.goal == "订明天的会议室" and not (last.checkpoint or {}).get(proactive_tasks.FOLLOWUP)


@pytest.mark.asyncio
async def test_another_im_conversation_does_not_see_the_summary(agent):
    from tests.test_personal_agent_channel_sessions import conversation
    from personal_agent.conversation.router import context as front
    factory = agent["factory"]
    await summary_sent_to_im(factory)
    with factory() as db:
        other = conversation(db, "bob")
        origin = {**ORIGIN, "msg": {**ORIGIN["msg"], "external_conversation_id": "bob"}}
        sent = admit(AgentContext(db, "owner", chat_id=other), "发吧", "bob", extra={"channel_origin": origin})["message_id"]
        assert "Sent to this IM conversation lately" not in front.turn_context(sent)
