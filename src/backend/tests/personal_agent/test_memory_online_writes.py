"""The wiki side of memory: seeding, the agent's page writes, the editor and injection.

Shares the workspace fixture and helpers with tests/test_personal_agent_file_memory.py
(the platform journal).
"""
import personal_agent.workspace.files as _pa_files
import personal_agent.workspace.prompting as _pa_prompting
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from personal_agent.models import PersonalAgent
from personal_agent.workspace import persona
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from tests.test_personal_agent_routing import sessions, personal_agent_on, turn, scripted  # noqa: F401
from tests.test_personal_agent_file_memory import page, versions, workspace  # noqa: F401


# ---------------------------------------------------------------- the wiki: seeding and who may write what

def test_seed_writes_schema_index_and_pinned_memory(workspace):
    from personal_agent.memory import inject, seed as memory_seed
    agent, root = workspace
    assert (root / "memory/_schema.md").read_text() == memory_seed.schema_text()
    assert (root / "memory/_index.md").read_text().startswith("# 记忆目录")
    memory = (root / "MEMORY.md").read_text()
    assert "## 置顶" in memory and inject.pinned(memory) == "" and "memory/_index.md" in memory   # an empty section injects nothing
    # Nobody may change the schema: seeding puts it back.
    (root / "memory/_schema.md").write_text("changed")
    persona.ensure_seeded(agent["id"])
    assert (root / "memory/_schema.md").read_text() == memory_seed.schema_text()


def test_editable_files_and_the_memory_write_path(workspace):
    agent, root = workspace
    for rel in ("USER.md", "MEMORY.md", "SOUL.md", "HEARTBEAT.md", "memory/_index.md", "memory/project-launch.md"):
        assert _pa_files.editable(rel), rel
    for rel in ("memory/_schema.md", "journal/2026-10-16.md", "memory/2026-10-16.md", "work/x/notes.md"):
        assert not _pa_files.editable(rel), rel
    # Memory files are written only through the memory store.
    for rel in ("USER.md", "MEMORY.md", "memory/project-launch.md"):
        with pytest.raises(PermissionError):
            _pa_files.write_file(agent["id"], rel, "x")


# ---------------------------------------------------------------- online writes from the agent's turn

def test_turn_writes_a_page_citing_its_source_id(workspace, sessions):
    from personal_agent.conversation.router import turn as router_turn
    agent, root = workspace
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "下周一发布，记一下", "page-write")
    sid = "conv-" + entry["message_id"]
    seen = []
    async def source(plan):
        seen.append(plan["turn"].source_id)
        seen.append(router_turn.with_source("<turn_context>\nx\n</turn_context>", plan["turn"].source_id))
    content = page("project-launch", "发布定在 2026-11-16 [src: " + sid + " | as-of: 2026-11-09]")
    observed = []
    async def capture(plan):
        observed.extend(plan["results"])
    asyncio.run(turn(entry["message_id"], scripted(
        source,
        ("pa_memory_search", {"query": "发布"}),
        ("pa_memory_write", {"path": "memory/project-launch.md", "content": content}),
        ("pa_memory_read", {"path": "memory/project-launch.md", "limit": 3}),
        capture, "记下了。")))
    assert seen[0] == sid
    assert seen[1].endswith("本轮来源 id：" + sid + "\n</turn_context>")
    assert observed[0] == {"pages": [], "lines": [], "journal": []}
    assert observed[1]["saved"] and observed[1]["changed"]
    assert observed[2]["content"] == "---\nname: project-launch\ndescription: Q4 发布计划" and observed[2]["more"]
    assert (root / "memory/project-launch.md").read_text() == content
    rows = versions(sessions, agent["id"], "memory/project-launch.md")
    assert [(r["writer"], r["source_id"], r["created"]) for r in rows] == [("online", sid, True)]
    # A new page waits under ## 待整理 until Dreaming files it.
    assert "## 待整理\n- [[project-launch]]：Q4 发布计划" in (root / "memory/_index.md").read_text()

    # The next turn finds it and corrects a fact in place.
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "改到周三了", "page-edit")
    later = "conv-" + entry["message_id"]
    observed.clear()
    asyncio.run(turn(entry["message_id"], scripted(
        ("pa_memory_search", {"query": "发布"}),
        ("pa_memory_edit", {"path": "memory/project-launch.md", "old_string": "- 发布定在 2026-11-16 [src: " + sid + " | as-of: 2026-11-09]",
            "new_string": "- 发布定在 2026-11-18；was 2026-11-16 until 2026-11-10 [src: " + later + " | as-of: 2026-11-10]"}),
        capture, "改好了。")))
    assert observed[0]["pages"][0]["path"] == "/workspace/memory/project-launch.md"
    assert observed[1]["saved"]
    assert "was 2026-11-16 until 2026-11-10" in (root / "memory/project-launch.md").read_text()
    assert versions(sessions, agent["id"], "memory/project-launch.md")[0]["source_id"] == later


def test_turn_page_write_is_refused_without_a_real_source(workspace, sessions):
    agent, root = workspace
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "记一下", "bad-source")
    observed = []
    async def capture(plan):
        observed.extend(plan["results"])
    asyncio.run(turn(entry["message_id"], scripted(
        ("pa_memory_write", {"path": "memory/project-launch.md", "content": page("project-launch", "发布 [src: conv-made-up | as-of: 2026-11-09]")}),
        ("pa_memory_write", {"path": "memory/project-launch.md", "content": page("project-launch", "发布在周一")}),
        ("pa_memory_write", {"path": "memory/_index.md", "content": "# 记忆目录\n"}),
        ("pa_memory_write", {"path": "memory/_schema.md", "content": "no rules"}),
        capture, "没有保存。")))
    assert observed == [{"error": "MemoryLintError"}, {"error": "MemoryLintError"}, {"error": "PermissionError"}, {"error": "PermissionError"}]
    assert not (root / "memory/project-launch.md").exists()


def test_remember_goes_to_user_and_pinned_memory_and_is_injected(workspace, sessions):
    from personal_agent.conversation.router import context as front
    agent, root = workspace
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "记住：总是用中文回答；发布前要通知财务", "longterm")
    memory = (root / "MEMORY.md").read_text()
    asyncio.run(turn(entry["message_id"], scripted(
        ("pa_memory_read", {"path": "USER.md"}),
        ("pa_memory_write", {"path": "USER.md", "content": "- 总是用中文回答"}),
        ("pa_memory_read", {"path": "MEMORY.md"}),
        ("pa_memory_edit", {"path": "MEMORY.md", "old_string": "## 置顶", "new_string": "## 置顶\n- 发布前要通知财务"}),
        "两条都记住了。",
    )))
    assert (root / "MEMORY.md").read_text() == memory.replace("## 置顶", "## 置顶\n- 发布前要通知财务")
    written = versions(sessions, agent["id"], "USER.md") + versions(sessions, agent["id"], "MEMORY.md")
    assert {r["writer"] for r in written if not r["created"]} == {"online"}   # besides the seeding baselines
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "What do you remember?", "next-turn")
    prompt = front.system_prompt(entry["message_id"])
    assert "总是用中文回答" in prompt and "发布前要通知财务" in prompt


def test_stale_model_replace_preserves_newer_file(workspace, sessions):
    from personal_agent.memory import layout, store
    from personal_agent.models import PersonalAgent
    agent, root = workspace
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "Update the memory", "stale")
    async def concurrent_edit(plan):
        with sessions() as db:
            store.write(db, db.get(PersonalAgent, agent["id"]), "MEMORY.md", "## 置顶\n- A newer decision.", None, writer=layout.USER_EDIT)
            db.commit()
    asyncio.run(turn(entry["message_id"], scripted(
        ("pa_memory_read", {"path": "MEMORY.md"}), concurrent_edit,
        ("pa_memory_write", {"path": "MEMORY.md", "content": "Stale replacement."}), "Done.",
    )))
    assert (root / "MEMORY.md").read_text() == "## 置顶\n- A newer decision."


def test_only_page_writes_wait_for_dreaming(workspace, sessions):
    from personal_agent.memory import state
    from personal_agent.models import PersonalAgent
    agent, root = workspace
    with sessions() as db:
        row = db.get(PersonalAgent, agent["id"])
        assert state.hold(row, "dreaming-run")
        db.commit()
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "记住：周五休假", "busy")
    observed = []
    async def capture(plan):
        observed.extend(plan["results"])
    asyncio.run(turn(entry["message_id"], scripted(
        ("pa_memory_read", {"path": "USER.md"}),
        ("pa_memory_write", {"path": "USER.md", "content": "- 周五休假"}),
        ("pa_memory_write", {"path": "memory/topic-vacation.md", "content": page("topic-vacation", "周五休假")}),
        capture, "记住了。")))
    assert observed[-1] == {"error": "MemoryConflict"}   # the page waits for the run
    assert "周五休假" in (root / "USER.md").read_text()   # the owner's words do not


# ---------------------------------------------------------------- the workspace editor

def put(sessions, path, content, etag):
    from personal_agent.api.schemas import WorkspaceFile
    from personal_agent.api.workspace import workspace_write
    with sessions() as db:
        response = workspace_write(WorkspaceFile(path=path, content=content, etag=etag), svc=AgentContext(db, "owner"))
    return response["data"]


def test_editor_saves_memory_through_the_store(workspace, sessions):
    agent, root = workspace
    current = _pa_files.read_file(agent["id"], "MEMORY.md")
    saved = put(sessions, "MEMORY.md", "## 置顶\n- 用户自己写的", current["etag"])
    assert saved["content"] == "## 置顶\n- 用户自己写的" and saved["version_id"].startswith("pamv_")
    rows = versions(sessions, agent["id"], "MEMORY.md")
    assert rows[0]["id"] == saved["version_id"] and rows[0]["writer"] == "user"
    # The owner writes pages without a source tag check on the source's existence; lint still runs.
    from personal_agent.memory.store import MemoryConflict, MemoryLintError
    with pytest.raises(MemoryLintError):
        put(sessions, "memory/project-launch.md", "no frontmatter", "")
    with pytest.raises(MemoryConflict):
        put(sessions, "MEMORY.md", "stale", current["etag"])
    # Persona files keep the plain write path.
    soul = _pa_files.read_file(agent["id"], "SOUL.md")
    assert put(sessions, "SOUL.md", "# SOUL", soul["etag"])["content"] == "# SOUL"
    assert versions(sessions, agent["id"], "SOUL.md") == []


def test_editor_page_saves_wait_for_dreaming(workspace, sessions):
    from fastapi import HTTPException
    from personal_agent.memory import state
    from personal_agent.memory.store import MemoryConflict
    from personal_agent.models import PersonalAgent
    agent, root = workspace
    with sessions() as db:
        assert state.hold(db.get(PersonalAgent, agent["id"]), "dreaming-run")
        db.commit()
    current = _pa_files.read_file(agent["id"], "USER.md")
    assert put(sessions, "USER.md", "- 新偏好", current["etag"])["content"] == "- 新偏好"
    with pytest.raises(MemoryConflict) as raised:
        put(sessions, "memory/project-launch.md", page("project-launch", "发布 [src: task-x | as-of: 2026-11-09]"), "")
    assert raised.value.status_code == 409
    with pytest.raises(HTTPException) as denied:
        put(sessions, "memory/_schema.md", "no rules", None)
    assert denied.value.status_code == 403
    assert [r["writer"] for r in versions(sessions, agent["id"], "USER.md") if r["writer"] != "system"] == ["user"]


# ---------------------------------------------------------------- what every turn sees

def test_injection_has_user_pinned_memory_and_index_head_only(workspace, sessions):
    agent, root = workspace
    from personal_agent.memory import store as memory_store
    with sessions() as db:   # through the store: a file written around it is not injected
        row = db.get(PersonalAgent, agent["id"])
        for rel, text in (("USER.md", "- 总是用中文回答\n"),
                          ("MEMORY.md", "# MEMORY.md\n\n旧的长文，不注入\n\n## 置顶\n- 发布前通知财务\n\n## 旧记录\n- 不注入的旧事实\n"),
                          ("memory/_index.md", "# 记忆目录\n\n## Where to start\n- [[project-launch]]：当前最重要的项目\n\n"
                                               "## 项目\n- [[project-launch]]：发布\n- [[project-crm]]：客户系统\n\n## 待整理\n- [[topic-x]]：新页面\n")):
            memory_store.write(db, row, rel, text, None, writer="system", check=False)
        db.commit()
    (root / "memory/project-launch.md").write_text(page("project-launch", "页面正文不注入 [src: task-x | as-of: 2026-11-09]"))
    (root / "journal").mkdir()
    (root / "journal" / (datetime.now(ZoneInfo("UTC")).date().isoformat() + ".md")).write_text("今天的流水账不注入")
    items = _pa_prompting.sections(agent["id"], rules_part="front_rules", seed_missing=False)
    text = _pa_prompting.render(items)
    assert "总是用中文回答" in text and "- 发布前通知财务" in text
    assert "旧的长文" not in text and "不注入的旧事实" not in text
    assert "## Where to start\n- [[project-launch]]：当前最重要的项目" in text and "## 项目（2 页）" in text and "## 待整理（1 页）" in text
    assert "[[project-crm]]" not in text and "页面正文不注入" not in text and "流水账" not in text
    paths = [item.get("path") for item in items if item["kind"] == "file"]
    assert "/workspace/memory/_index.md" in paths and not any("journal" in (p or "") for p in paths)
    assert "本轮来源 id" in text and "was X until" in text and "pa_memory_search" in text
    assert "memory/YYYY-MM-DD" not in text


def test_task_writes_a_page_as_its_own_source(workspace, sessions, monkeypatch):
    import time
    from types import SimpleNamespace
    from personal_agent.models import PersonalWorkItem
    from personal_agent.tools import tools
    agent, root = workspace
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tools.email.register_email_tools", lambda *args: None)
    with sessions() as db:
        task = AgentContext(db, "owner").tasks.submit("Look up the CRM vendor", "task-page")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        db.commit()
    kit = {}
    class Toolkit:
        def append(self, tool): kit[tool._func.__name__] = tool._func
    tools.register_personal_tools(Toolkit(), SimpleNamespace(profile_ref=task["id"], current_user_id="owner"))
    sid = "task-" + task["id"]
    content = page("org-acme", "CRM 供应商是 Acme（agent 观察）[src: " + sid + " | as-of: 2026-11-09]", "CRM 供应商")
    async def run():
        assert json.loads((await kit["pa_memory_search"]("Acme")).content[0].text)["pages"] == []
        await kit["pa_write_file"]("/workspace/memory/org-acme.md", content)
        await kit["pa_read_file"]("/workspace/memory/org-acme.md")
        await kit["pa_edit_file"]("/workspace/memory/org-acme.md", "（agent 观察）", "")
        with pytest.raises(PermissionError):
            await kit["pa_write_file"]("/workspace/memory/_index.md", "# 记忆目录\n")
    asyncio.run(run())
    rows = versions(sessions, agent["id"], "memory/org-acme.md")
    assert [(r["writer"], r["source_id"]) for r in rows] == [("online", sid), ("online", sid)]
    assert "（agent 观察）" not in (root / "memory/org-acme.md").read_text()
    assert "[[org-acme]]" in (root / "memory/_index.md").read_text()


def test_source_ids_are_not_mistaken_for_keys():
    from personal_agent.memory.redact import holds_secret, redact
    assert not holds_secret("[src: task-0123456789abcdef0123456789abcdef | as-of: 2026-11-09]")
    assert redact("key sk-0123456789abcdef0123") == "key [REDACTED]"
