"""The memory wiki's shared foundation: page lint, the single write path, versions, snapshots."""
import pytest

import personal_agent.workspace.files as _pa_files
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from personal_agent.memory import inject, layout, lint, page, search, seed, snapshots, state, store, versions
from personal_agent.models import PersonalAgent
from tests.test_personal_agent_routing import sessions  # noqa: F401


def good_page(name="person-zhang-san", src="conv-x", extra=""):
    return ("---\nname: " + name + "\ndescription: 用户的直属上级；审批前读\naliases: [\"张三\"]\n"
            "type: " + name.split("-")[0] + "\nstatus: active\nupdated: 2026-11-09\n---\n"
            "## 角色\n- 直属上级 [src: " + src + " | as-of: 2026-10-02]\n" + extra)


@pytest.fixture
def agent(sessions, monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        view = AgentContext(db, "owner").identity.ensure()
        message = admit(AgentContext(db, "owner"), "张三是我直属领导", "memory-src")
    _pa_files.local_workspace(view["id"]).mkdir(parents=True, exist_ok=True)
    seed.seed(view["id"])
    return view["id"], "conv-" + message["message_id"]


def test_page_parse_and_sources():
    parsed = page.parse(good_page(src="conv-a"))
    assert parsed.meta["type"] == "person" and parsed.aliases == ["张三"]
    assert page.sources(good_page(src="conv-a")) == ["conv-a"]
    assert page.stub_target("MOVED TO [[person-li]]\n") == "person-li"


def test_lint_refuses_bad_pages():
    assert lint.check_page("memory/person-zhang-san.md", good_page(), lambda sid: True) == []
    rules = {f.rule for f in lint.check_page("memory/person-zhang-san.md", good_page(extra="- 没有来源\n"), None)}
    assert "fact-source" in rules
    assert {f.rule for f in lint.check_page("memory/Zhang.md", "x")} == {"file-name"}
    assert "secret" in {f.rule for f in lint.check_page("memory/person-zhang-san.md", good_page(extra="- password: hunter2 [src: conv-x | as-of: 2026-10-02]\n"))}
    assert "fact-source" in {f.rule for f in lint.check_page("memory/person-zhang-san.md", good_page(), lambda sid: False)}


def test_seed_writes_schema_and_index_and_restores_schema(agent):
    ident, _ = agent
    root = _pa_files.local_workspace(ident)
    assert (root / "memory/_schema.md").read_text() == seed.schema_text()
    (root / "memory/_schema.md").write_text("tampered")
    seed.seed(ident)
    assert (root / "memory/_schema.md").read_text() == seed.schema_text()


def test_online_write_lints_versions_and_lists_new_page(agent, sessions):
    ident, src = agent
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        with pytest.raises(store.MemoryLintError):
            store.write(db, row, "memory/person-zhang-san.md", good_page(src="conv-missing"), "", writer=layout.ONLINE, source_id=src)
        saved = store.write(db, row, "memory/person-zhang-san.md", good_page(src=src), "", writer=layout.ONLINE, source_id=src)
        db.commit()
        assert saved["version"].writer == layout.ONLINE
        index = store.read(ident, layout.INDEX)
        assert layout.PENDING_HEADING in index and "[[person-zhang-san]]" in index
        assert {v.path for v in versions.listing(db, ident)} == {layout.INDEX, "memory/person-zhang-san.md"}
        with pytest.raises(PermissionError):
            store.write(db, row, layout.SCHEMA, "x", None, writer=layout.ONLINE, source_id=src)
        with pytest.raises(PermissionError):
            store.write(db, row, layout.INDEX, "x", None, writer=layout.ONLINE, source_id=src)


def test_lease_blocks_online_and_editor_writes(agent, sessions):
    ident, src = agent
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        assert state.hold(row, "run-1")
        with pytest.raises(store.MemoryConflict):
            store.write(db, row, "memory/person-zhang-san.md", good_page(src=src), "", writer=layout.ONLINE, source_id=src)
        # Dreaming never writes USER.md or MEMORY.md: an owner's "记住" does not wait for it.
        store.write(db, row, "MEMORY.md", "# MEMORY\n## 置顶\n- 周五下午不开会\n", None, writer=layout.ONLINE, source_id=src)
        store.write(db, row, "memory/person-zhang-san.md", good_page(src=src), None, writer=layout.ANALYZER, source_id=src)
        state.release(row, "run-1")
        assert not state.busy(row)


def test_revert_one_change(agent, sessions):
    ident, src = agent
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        store.write(db, row, "memory/person-zhang-san.md", good_page(src=src), "", writer=layout.ONLINE, source_id=src)
        changed = store.write(db, row, "memory/person-zhang-san.md", good_page(src=src, extra="- 爱喝茶 [src: " + src + " | as-of: 2026-10-03]\n"),
                              None, writer=layout.USER_EDIT)
        db.commit()
        store.revert(db, row, changed["version"].id)
        db.commit()
        assert store.read(ident, "memory/person-zhang-san.md") == good_page(src=src)


def test_snapshot_restore_and_search(agent, sessions):
    ident, src = agent
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        store.write(db, row, "memory/person-zhang-san.md", good_page(src=src), "", writer=layout.ONLINE, source_id=src)
        snapshots.take(ident, "before")
        store.write(db, row, "memory/project-q4-budget.md", good_page("project-q4-budget", src), "", writer=layout.ONLINE, source_id=src)
        snapshots.restore(db, row, "before")
        db.commit()
    assert store.read(ident, "memory/project-q4-budget.md") is None
    assert [s["id"] for s in snapshots.listing(ident)] == ["before"]
    found = search.search(ident, "张三")
    assert found["pages"][0]["path"].endswith("person-zhang-san.md")


def test_injection_keeps_pinned_and_index_head_only():
    memory = "# MEMORY\n## 置顶\n- 周五下午不开会\n\n## 旧笔记\n- 随手记\n"
    assert inject.pinned(memory) == "## 置顶\n- 周五下午不开会"
    index = "# 记忆目录\n\n## Where to start\n- 先看 [[person-a]]\n\n## 人\n- [[person-a]]：a\n- [[person-b]]：b\n"
    head = inject.index_head(index)
    assert "先看" in head and "## 人（2 页）" in head and "：b" not in head


def test_a_model_write_may_not_duplicate_a_fact_line(agent, sessions):
    ident, src = agent
    line = "- 爱喝茶 [src: " + src + " | as-of: 2026-10-03]\n"
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        store.write(db, row, "memory/person-zhang-san.md", good_page(src=src, extra=line), "", writer=layout.ONLINE, source_id=src)
        with pytest.raises(store.MemoryLintError) as refused:
            store.write(db, row, "memory/person-zhang-san.md", good_page(src=src, extra=line + "## 其他\n" + line), None,
                        writer=layout.ORGANIZER)
        assert refused.value.findings[0].rule == "duplicate-fact"
        # The owner's own edit is not judged.
        store.write(db, row, "memory/person-zhang-san.md", good_page(src=src, extra=line + "## 其他\n" + line), None,
                    writer=layout.USER_EDIT)
        db.commit()
    findings = lint.check_wiki(store.wiki(ident), seed.schema_text())
    assert "duplicate-fact" in {f.rule for f in findings if f.level == lint.WARN}
