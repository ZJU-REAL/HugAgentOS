"""Dreaming sources: rendering from the database, genesis replay, settings and tools."""
import pytest

from personal_agent.context import AgentContext
from personal_agent.memory import layout, state as memory_state, store
from personal_agent.memory.dreaming import collect, render, runs
from personal_agent.memory.dreaming.tools import Toolbox
from personal_agent.models import PersonalAgent
from tests.personal_agent.dreaming_support import (  # noqa: F401
    NOW, Script, agent, personal_agent_on, call, conversation, fact, index_text, page_text, receipt, run_view, say, sessions, tick, write)

STEPS = [
    {"kind": "assistant", "blocks": [{"type": "thinking", "thinking": "私下的推理"},
                                     {"type": "tool_call", "id": "c1", "name": "web_search", "input": '{"query": "Q4 预算"}'}]},
    {"kind": "tool_result", "blocks": [{"type": "tool_result", "id": "c1", "name": "web_search", "output": "甲" * 6000, "state": "success"}]},
    {"kind": "assistant", "blocks": [{"type": "text", "text": "查到了。服务器 password: hunter2"}]},
]


def test_a_conversation_renders_with_tools_without_thinking_and_redacted(agent, sessions):
    ident = agent
    sid = conversation(sessions, "帮我查 Q4 预算", "查到了。", steps=STEPS)
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        from core.db.models import ChatMessage
        out = render.render(db, row, db.get(ChatMessage, sid.split("-", 1)[1]))
    text = out["text"]
    assert out["sid"] == sid and text.startswith("SOURCE " + sid)
    assert "] USER:\n帮我查 Q4 预算" in text and "TOOL CALL web_search:\nquery: Q4 预算" in text
    assert "TOOL RESULT (web_search):" in text and "[…截断，原文共 18000 字节]" in text
    assert "私下的推理" not in text and "hunter2" not in text and "[REDACTED]" in text
    assert render.split("a\n" * 70_000, 120_000)[0].endswith("a") and len(render.split("x" * 250_000)) == 3


def test_a_long_source_is_split_and_the_batch_is_capped(agent, sessions):
    ident = agent
    steps = [{"kind": "assistant", "blocks": [{"type": "tool_call", "id": "c" + str(n), "name": "read", "input": "{}"}]} for n in range(50)]
    steps += [{"kind": "tool_result", "blocks": [{"type": "tool_result", "id": "c" + str(n), "name": "read", "output": "很长的结果\n" * 1300}]} for n in range(50)]
    big = conversation(sessions, "长对话", "读完了", steps=steps)
    for n in range(45):
        conversation(sessions, "第 " + str(n) + " 条", "好")
    claimed = runs.claim(sessions, ident, NOW)
    batch = collect.prepare(sessions, ident, claimed[0], claimed[1])
    assert len(batch.sources) == collect.MAX_SOURCES and batch.receipts[0] == big.split("-", 1)[1]
    first = [s for s in batch.sources if s["sid"] == big]
    assert len(first) == 2 and first[0]["parts"] == 2 and all(len(s["text"]) <= render.PART_MAX_CHARS for s in first)


def test_genesis_replays_dreamed_receipts_then_finishes(agent, sessions):
    ident = agent
    sid = conversation(sessions, "旧对话：张三是上级", "好")
    with sessions() as db:
        from core.db.models import ChatMessage
        row = db.get(ChatMessage, sid.split("-", 1)[1])
        row.extra_data = {**row.extra_data, "personal_file_memory": {**row.extra_data["personal_file_memory"], "dreamed": True}}
        memory_state.request_genesis(db.get(PersonalAgent, ident), "2020-01-01T00:00:00+00:00")
        db.commit()
    script = Script(analyzer=[[[say('{"summary": "回放"}')]]], organizer=[[say('{"index_entries": 0}')]])
    tick(sessions, script)
    assert [kind for kind, _ in script.tasks] == ["analyzer", "organizer"]
    with sessions() as db:
        genesis = memory_state.genesis(db.get(PersonalAgent, ident))
    assert genesis["status"] == "done" and genesis["cursor"]["id"] == sid.split("-", 1)[1]
    import personal_agent.workspace.files as _pa_files
    assert "创世回放：已完成" in (_pa_files.local_workspace(ident) / "DREAMS.md").read_text()


def test_settings_validate_and_view_keeps_its_fields(agent, sessions):
    with pytest.raises(ValueError):
        runs.validate({"hour": 24}, {})
    with pytest.raises(ValueError):
        runs.validate({"unknown": 1}, {})
    assert runs.validate({"model_provider_id": "cheap"}, {})["model_provider_id"] == "cheap"
    assert runs.provider({"model_provider_id": "main", "dreaming": {"model_provider_id": "cheap"}}) == "cheap"
    assert runs.provider({"model_provider_id": "main"}) == "main"
    with sessions() as db:
        AgentContext(db, "owner").identity.configure({"dreaming": {"hour": 5}})
    # There is no switch: an old client's "enabled" is refused, an old config's is ignored.
    with pytest.raises(ValueError):
        runs.validate({"enabled": False}, {})
    assert "enabled" not in runs.options({"dreaming": {"enabled": False, "hour": 4}})
    view = run_view(sessions)
    assert "enabled" not in view
    for key in ("hour", "timezone", "next_run", "run_id", "status", "attempts", "next_retry", "error",
                "outcomes", "completed_at", "pending", "genesis", "model_provider_id"):
        assert key in view
    assert view["hour"] == 5
    with sessions() as db:
        assert runs.run_now(db, "owner", NOW)["status"] == "pending"


def test_tools_stay_inside_memory_and_refuse_bad_writes(agent, sessions):
    ident = agent
    sid = conversation(sessions, "x", "y")
    claimed = runs.claim(sessions, ident, NOW)
    box = Toolbox(sessions, ident, claimed[0], claimed[1], layout.ANALYZER, source_id=sid, sources={"sources/1.txt": "a\nb"})
    assert box.call("read", '{"path": "sources/1.txt"}')[0].endswith("\tb")
    assert box.call("write", '{"path": "sources/1.txt", "content": "x"}')[1] is False
    assert box.call("read", '{"path": "USER.md"}')[1] is False
    assert box.call("write", '{"path": "MEMORY.md", "content": "x"}')[1] is False
    assert box.call("write", '{"path": "memory/_schema.md", "content": "x"}')[1] is False
    out, ok = box.call("write", '{"path": "memory/person-li.md", "content": "no frontmatter"}')
    assert not ok and "frontmatter" in out
    ok = box.call("write", '{"path": "memory/person-li.md", "content": ' + __import__("json").dumps(page_text("person-li", ["## 角色", fact("同事", sid)])) + '}')[1]
    assert ok and box.written == ["memory/person-li.md"]
    assert box.call("grep", '{"pattern": "同事"}')[0].startswith("memory/person-li.md:")
    assert "memory/person-li.md" in box.call("glob", '{"pattern": "memory/person-*.md"}')[0]
    assert box.call("delete", "{}")[1] is False
    assert box.call("write", '{"path": "memory/person-li.md", "content": "MOVED TO [[person-wang]]\\n"}')[1] is False


def test_a_page_flipped_back_and_forth_is_frozen_for_the_step(agent, sessions):
    import json
    ident = agent
    sid = conversation(sessions, "x", "y")
    claimed = runs.claim(sessions, ident, NOW)
    box = Toolbox(sessions, ident, claimed[0], claimed[1], layout.ANALYZER, source_id=sid)
    base, extra = page_text("person-li", ["## 角色", fact("同事", sid)]), "- 爱喝茶 [src: " + sid + " | as-of: 2026-10-03]\n"
    assert box.call("write", json.dumps({"path": "memory/person-li.md", "content": base}))[1]
    edit = lambda old, new: box.call("edit", json.dumps({"path": "memory/person-li.md", "old_string": old, "new_string": new}))
    line = fact("同事", sid)
    assert edit(line, line + extra)[1]
    assert edit(line + extra, line)[1]             # undoing itself once is allowed
    assert edit(line, line + extra)[1] is False    # the second return is refused
    assert box.call("write", json.dumps({"path": "memory/person-li.md", "content": base + "## 备注\n" + extra}))[1]
    assert store.read(ident, "memory/person-li.md") == base + "## 备注\n" + extra
