"""The Dreaming pipeline end to end with a scripted model: analyze, organize, finish, roll back."""
import pytest

from personal_agent.memory import layout, snapshots, state as memory_state, store, versions
from personal_agent.memory.dreaming import runs
from personal_agent.models import PersonalAgent
from tests.personal_agent.dreaming_support import (  # noqa: F401
    DAY, NOW, Script, agent, personal_agent_on, call, conversation, fact, index_text, page_text, receipt, run_view, say, sessions, tick, write)

ZHANG = "memory/person-zhang-san.md"
BUDGET = "memory/project-q4-budget.md"


def test_run_creates_and_updates_pages_and_verifies_online_change(agent, sessions):
    ident = agent
    sid = conversation(sessions, "张三是我直属领导。Q4 云资源预算定成 55 万。", "好的，记下了。")
    # The day's online write: one true line and one the agent guessed.
    online = page_text("person-zhang-san", ["## 角色", fact("直属上级", sid), fact("喜欢喝茶", sid)])
    write(sessions, ident, ZHANG, online, writer=layout.ONLINE, source_id=sid)

    def verify(messages):
        return [call("edit", path=ZHANG, old_string=fact("喜欢喝茶", sid), new_string=fact("喜欢喝茶（agent 观察）", sid)),
                call("write", path=BUDGET, content=page_text("project-q4-budget", ["## 金额", fact("云资源 55 万", sid)]))]

    index = index_text([("person-zhang-san", "直属上级"), ("project-q4-budget", "Q4 预算")])
    script = Script(
        analyzer=[[[call("read", path="sources/1.txt")], verify,
                   [say('{"summary": "确认上级，补上预算", "pages_written": [], "online_changes": {"kept": 1, "revised": 1, "reverted": 0}}')]]],
        organizer=[[call("write", path=layout.INDEX, content=index)], [say('{"index_entries": 2, "changes": ["重建索引"]}')]])
    tick(sessions, script)

    source = script.seen[0]
    assert source.splitlines()[0].strip().endswith("SOURCE " + sid + " · " + DAY + "T11:00:00+08:00 · 主对话")
    assert "USER:" in source and "AGENT:" in source and "张三是我直属领导" in source
    assert "=== 白天的记忆改动 ===" in source and "喜欢喝茶" in source and "sources/1.txt" in script.tasks[0][1]
    assert "（agent 观察）" in store.read(ident, ZHANG) and "云资源 55 万" in store.read(ident, BUDGET)
    assert "[[project-q4-budget]]" in store.read(ident, layout.INDEX)
    import personal_agent.workspace.files as _pa_files
    report = (_pa_files.local_workspace(ident) / "DREAMS.md").read_text()
    assert "确认上级，补上预算" in report and "保留 1，修正 1，撤回 0" in report
    assert "新建：memory/project-q4-budget.md" in report and "修改：memory/person-zhang-san.md" in report
    assert "新页面只有 1 个来源" in report  # a lint warning goes into the report, it does not block
    assert receipt(sessions, sid)["dreamed"] is True
    status = run_view(sessions)
    assert status["status"] == "done" and status["pending"] == 0 and "来源 1/1" in status["outcomes"]
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        assert not memory_state.busy(row)
        writers = {v.writer for v in versions.listing(db, ident, run_id=status["run_id"])}
        assert writers == {layout.ANALYZER, layout.ORGANIZER}
    assert snapshots.listing(ident)[0]["id"].startswith(status["run_id"])


def test_organizer_merges_a_duplicate_through_a_stub(agent, sessions):
    ident = agent
    sid = conversation(sessions, "张三和 zhangsan 是同一个人", "明白")
    write(sessions, ident, ZHANG, page_text("person-zhang-san", ["## 角色", fact("直属上级", sid)], aliases=["张三"]))
    other = page_text("person-zhangsan", ["## 联系", fact("微信 zs001", sid)], aliases=["zhangsan"])
    write(sessions, ident, "memory/person-zhangsan.md", other)
    write(sessions, ident, layout.INDEX, index_text([("person-zhang-san", "上级"), ("person-zhangsan", "同一人")]))
    merged = page_text("person-zhang-san", ["## 角色", fact("直属上级", sid), "## 联系", fact("微信 zs001", sid)], aliases=["张三", "zhangsan"])
    script = Script(
        analyzer=[[[say('{"summary": "同一个人", "pages_written": [], "online_changes": {}}')]]],
        organizer=[[call("write", path=ZHANG, content=merged), call("write", path="memory/person-zhangsan.md", content="MOVED TO [[person-zhang-san]]\n")],
                   [call("write", path=layout.INDEX, content=index_text([("person-zhang-san", "上级")]))],
                   [say('{"index_entries": 1, "changes": ["合并 person-zhangsan"]}')]])
    tick(sessions, script)
    assert store.read(ident, "memory/person-zhangsan.md") is None
    assert "微信 zs001" in store.read(ident, ZHANG)
    import personal_agent.workspace.files as _pa_files
    report = (_pa_files.local_workspace(ident) / "DREAMS.md").read_text()
    assert "合并：person-zhangsan → person-zhang-san" in report and "合并 person-zhangsan" in report
    assert run_view(sessions)["status"] == "done"


def test_a_blocking_lint_finding_rolls_the_run_back(agent, sessions):
    ident = agent
    sid = conversation(sessions, "Q4 预算 55 万", "好")
    before = store.wiki(ident)
    script = Script(
        analyzer=[[[call("write", path=BUDGET, content=page_text("project-q4-budget", ["## 金额", fact("55 万", sid)]))],
                   [say('{"summary": "x"}')]]],
        # The organizer leaves the new page out of the index: a blocking finding.
        organizer=[[call("write", path=layout.INDEX, content=index_text([]))], [say('{"index_entries": 0, "changes": []}')]])
    tick(sessions, script)
    assert store.wiki(ident) == before
    assert receipt(sessions, sid).get("dreamed") is not True
    status = run_view(sessions)
    assert status["status"] == "retry" and status["error"].startswith("LintBlocked") and "project-q4-budget" in status["error"]
    import personal_agent.workspace.files as _pa_files
    assert "记忆已恢复到这次整理之前" in (_pa_files.local_workspace(ident) / "DREAMS.md").read_text()
    with sessions() as db:
        assert not memory_state.busy(db.get(PersonalAgent, ident))


def test_the_lease_blocks_online_writes_and_a_lost_lease_stops_the_run(agent, sessions):
    ident = agent
    sid = conversation(sessions, "记一下", "好")
    # Someone else holds the lease: no run starts.
    with sessions() as db:
        assert memory_state.hold(db.get(PersonalAgent, ident), "editor")
        db.commit()
    tick(sessions, Script())
    assert run_view(sessions)["status"] in (None, "pending")
    with sessions() as db:
        memory_state.release(db.get(PersonalAgent, ident), "editor")
        db.commit()

    seen = {}

    def online_write_then_steal(messages):
        with pytest.raises(store.MemoryConflict):
            write(sessions, ident, BUDGET, page_text("project-q4-budget", ["## 金额", fact("55 万", sid)]), writer=layout.ONLINE, source_id=sid)
        with sessions() as db:
            row = db.get(PersonalAgent, ident)
            row.config = {**row.config, memory_state.LEASE: {"owner": "thief", "until": 4e9}}
            db.commit()
        seen["stolen"] = True
        return [call("write", path=BUDGET, content=page_text("project-q4-budget", ["## 金额", fact("55 万", sid)]))]

    tick(sessions, Script(analyzer=[[online_write_then_steal]]), now=NOW + 1)
    assert seen["stolen"] and store.read(ident, BUDGET) is None
    status = run_view(sessions)
    assert status["status"] == "retry" and status["error"] == "LeaseLost"
    with sessions() as db:
        # The thief's lease is not released or rolled over by the stopped run.
        assert (db.get(PersonalAgent, ident).config[memory_state.LEASE])["owner"] == "thief"


def test_retry_after_a_model_failure_is_idempotent(agent, sessions):
    ident = agent
    sid = conversation(sessions, "Q4 预算 55 万", "好")
    down = Script(analyzer=[[TimeoutError("model offline")], [TimeoutError("model offline")]])
    tick(sessions, down)
    status = run_view(sessions)
    assert status["status"] == "retry" and status["attempts"] == 1 and receipt(sessions, sid).get("dreamed") is not True
    tick(sessions, Script(), now=NOW + 30)   # still backing off
    assert run_view(sessions)["attempts"] == 1

    good = Script(
        analyzer=[[[call("write", path=BUDGET, content=page_text("project-q4-budget", ["## 金额", fact("55 万", sid)]))],
                   [call("edit", path=layout.INDEX, old_string="## Where to start", new_string="## Where to start\n\n## 待整理\n- [[project-q4-budget]]：预算")],
                   [say('{"summary": "预算"}')]]],
        organizer=[[call("write", path=layout.INDEX, content=index_text([("project-q4-budget", "预算")]))], [say('{"index_entries": 1}')]])
    tick(sessions, good, now=NOW + 120)
    status = run_view(sessions)
    assert status["status"] == "done" and status["attempts"] == 2 and receipt(sessions, sid)["dreamed"] is True
    import personal_agent.workspace.files as _pa_files
    path = _pa_files.local_workspace(ident) / "DREAMS.md"
    report = path.read_text()
    assert report.count("<!-- pa-dream:" + status["run_id"] + " -->") == 1
    assert report.count("<!-- pa-dream:" + status["run_id"] + "-attempt-1 -->") == 1
    # Nothing pending: later ticks start no run and change nothing.
    tick(sessions, Script(), now=NOW + 200)
    tick(sessions, Script(), now=NOW + 86400)
    assert path.read_text() == report and run_view(sessions)["run_id"] == status["run_id"]
    with sessions() as db:
        assert versions.listing(db, ident, path=BUDGET)[0].writer == layout.ANALYZER


def test_owner_remember_lines_are_only_suggested(agent, sessions):
    ident = agent
    conversation(sessions, "记住：周报每周五下午发给张三", "好的")
    script = Script(analyzer=[[[say('{"summary": "周报"}')]]],
                    organizer=[[say('{"index_entries": 0, "changes": []}')]])
    tick(sessions, script)
    import personal_agent.workspace.files as _pa_files
    report = (_pa_files.local_workspace(ident) / "DREAMS.md").read_text()
    assert "建议你确认后写进 USER.md 或 MEMORY.md 置顶" in report and "“周报每周五下午发给张三”" in report
    assert "周报" not in (store.read(ident, layout.USER) or "") + (store.read(ident, layout.MEMORY) or "")


def test_a_backlog_waits_for_the_slot_and_run_now_wakes_the_loop(agent, sessions, monkeypatch):
    import asyncio
    from personal_agent.memory.dreaming import wake
    ident = agent
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        slot, following = runs.schedule(row.config, NOW)
        row.config = {**row.config, "dreaming_state": {"slot": slot}}
        db.commit()
    for n in range(40):
        conversation(sessions, "第 " + str(n) + " 条", "好")
    # Only the slot, a continuation, genesis and "run now" start a run: 40 pending sources do not.
    assert runs.claim(sessions, ident, NOW) is None
    assert runs.plan(sessions, NOW) == ([], following)
    monkeypatch.setattr("personal_agent.memory.dreaming.wake.redis_configured", lambda: False)

    async def woken():
        waiting = asyncio.ensure_future(wake.wait(60))
        await asyncio.sleep(0.05)
        with sessions() as db:
            runs.run_now(db, db.get(PersonalAgent, ident).user_id, now=NOW)
        await asyncio.wait_for(waiting, 2)
    asyncio.run(woken())
    assert runs.plan(sessions, NOW)[0] == [ident]
    claimed = runs.claim(sessions, ident, NOW)
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        assert claimed and runs.run_state(runs.current(db, row))["reason"] == "manual" and memory_state.busy(row)


def test_due_times_follow_the_slot_the_retry_and_the_lease(agent, sessions):
    ident = agent
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        slot, following = runs.schedule(row.config, NOW)
        assert runs.due_at(row, None, {}, NOW) == NOW                # this slot is still unspent
        row.config = {**row.config, "dreaming_state": {"slot": slot}}
        assert runs.due_at(row, None, {}, NOW) == following
        assert runs.due_at(row, object(), {"status": "retry", "next_retry": NOW + 120}, NOW) == NOW + 120
        assert runs.due_at(row, object(), {"status": "failed", "started_at": NOW}, NOW) == following
        assert runs.due_at(row, None, {"continuation": True, "status": "done"}, NOW) == NOW
        row.status = "paused"
        assert runs.due_at(row, None, {}, NOW) is None
