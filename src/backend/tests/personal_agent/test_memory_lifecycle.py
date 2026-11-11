"""Pages nobody uses are archived, then deleted; use brings them back; search ranks archived last."""
from datetime import datetime, timedelta, timezone

import pytest

import personal_agent.workspace.files as _pa_files
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from personal_agent.memory import layout, lifecycle, search, seed, store, usage, versions
from personal_agent.memory.models import PersonalMemoryUsage
from personal_agent.models import PersonalAgent
from tests.test_personal_agent_routing import sessions  # noqa: F401

START = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
REL = "memory/topic-lark-cli.md"


def body(name, src, status="active", day="2026-11-10"):
    return ("---\nname: " + name + "\ndescription: 测试；用到时读\naliases: [\"x\"]\ntype: topic\nstatus: " + status
            + "\nupdated: " + day + "\n---\n## 状态\n- 已安装 [src: " + src + " | as-of: " + day + "]\n")


@pytest.fixture
def agent(sessions, monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        view = AgentContext(db, "owner").identity.ensure()
        message = admit(AgentContext(db, "owner"), "装了 lark-cli", "life-src")
    _pa_files.local_workspace(view["id"]).mkdir(parents=True, exist_ok=True)
    seed.seed(view["id"])
    src = "conv-" + message["message_id"]
    with sessions() as db:
        store.write(db, db.get(PersonalAgent, view["id"]), REL, body("topic-lark-cli", src), "", writer=layout.ONLINE, source_id=src)
        db.commit()
    return view["id"], src


def run(sessions, ident, days):
    now = START + timedelta(days=days)
    with sessions() as db:
        out = lifecycle.apply(db, db.get(PersonalAgent, ident), now.date(), run_id="run-x", now=now)
        db.commit()
    return out


def status(ident):
    from personal_agent.memory import page
    return page.parse(store.read(ident, REL)).meta["status"]


def test_unused_page_is_archived_then_deleted_and_can_be_restored(agent, sessions):
    ident, _src = agent
    assert run(sessions, ident, 0) == {"archived": [], "restored": [], "deleted": []}
    assert run(sessions, ident, 59)["archived"] == []
    assert run(sessions, ident, 60)["archived"] == ["topic-lark-cli"] and status(ident) == "archived"
    assert run(sessions, ident, 149)["deleted"] == []
    assert run(sessions, ident, 150)["deleted"] == ["topic-lark-cli"]
    assert store.read(ident, REL) is None
    with sessions() as db:
        assert db.get(PersonalMemoryUsage, (ident, REL)) is None
        gone = versions.latest(db, ident, REL)
        assert gone.after_text is None and store.revertable(gone, None)
        store.revert(db, db.get(PersonalAgent, ident), gone.id)
        db.commit()
    assert status(ident) == "archived" and "[[topic-lark-cli]]" in store.read(ident, layout.INDEX)


def test_use_keeps_a_page_alive_and_brings_an_archived_one_back(agent, sessions):
    ident, _src = agent
    run(sessions, ident, 0)
    with sessions() as db:
        usage.mark(db, ident, [REL], START + timedelta(days=50))
        db.commit()
    assert run(sessions, ident, 100)["archived"] == []
    assert run(sessions, ident, 110)["archived"] == ["topic-lark-cli"]
    usage.touch(ident, ["/workspace/" + REL])     # the agent reads it again (today, after the archive)
    with sessions() as db:
        row = db.get(PersonalMemoryUsage, (ident, REL))
        row.last_used_at = START + timedelta(days=120)
        db.commit()
    assert run(sessions, ident, 121)["restored"] == ["topic-lark-cli"] and status(ident) == "active"


def test_search_ranks_archived_pages_last_and_marks_hits_used(agent, sessions):
    ident, src = agent
    with sessions() as db:
        store.write(db, db.get(PersonalAgent, ident), "memory/topic-a-old.md",
                    body("topic-a-old", src, status="archived"), "", writer=layout.ONLINE, source_id=src)
        db.commit()
    found = search.search(ident, "已安装", used=True)
    assert [hit["path"] for hit in found["lines"]] == ["/workspace/" + REL, "/workspace/memory/topic-a-old.md"]
    with sessions() as db:
        assert db.get(PersonalMemoryUsage, (ident, REL)).uses == 1
    search.search(ident, "已安装")                  # Dreaming's own searches do not count
    with sessions() as db:
        assert db.get(PersonalMemoryUsage, (ident, REL)).uses == 1


def test_set_status_only_touches_the_frontmatter():
    text = body("topic-lark-cli", "conv-x") + "- status: 正常 [src: conv-x | as-of: 2026-11-10]\n"
    changed = lifecycle.set_status(text, "archived")
    assert "status: archived" in changed and "- status: 正常" in changed
