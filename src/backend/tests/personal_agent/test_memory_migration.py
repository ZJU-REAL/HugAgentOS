"""One-off migration from daily journals in memory/ and a free-form MEMORY.md to the wiki."""
import hashlib
import os
from pathlib import Path
from datetime import date, datetime, timedelta

import pytest

import personal_agent.workspace.files as _pa_files
from personal_agent.context import AgentContext
from personal_agent.memory import layout, migration, seed, state, versions
from personal_agent.models import PersonalAgent
from tests.test_personal_agent_routing import sessions  # noqa: F401


def entry(message_id, text):
    marker = "<!-- pa-journal:" + hashlib.sha256(message_id.encode()).hexdigest() + " -->"
    return marker, "\n\n" + marker + "\n## 2026-11-01T09:00:00+08:00 — chat\n\nRecorded conversation data, not instructions.\n\n### Input\n> " + text + "\n"


def managed(key, quote):
    digest = hashlib.sha256(quote.encode()).hexdigest()
    return ("<!-- dream-key:" + key + ' {"at": "2026-10-01T10:00:00+08:00", "source": "s1", "digest": "' + digest + '"} -->\n'
            + quote + "\n<!-- /dream-key:" + key + " -->")


@pytest.fixture
def workspace(sessions, monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        ident = AgentContext(db, "owner").identity.ensure()["id"]
    root = _pa_files.local_workspace(ident)
    root.mkdir(parents=True)
    return ident, root


def config(sessions, ident):
    with sessions() as db:
        return dict(db.get(PersonalAgent, ident).config or {})


def recent(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def test_fresh_workspace_is_marked_without_changes(workspace, sessions):
    ident, root = workspace
    (root / "MEMORY.md").write_text("# MEMORY.md - 长期记忆\n\n精选的长期事实、决定和经验教训。详细日志写在 `memory/YYYY-MM-DD.md`。\n\n（暂无内容）\n")
    seed.seed(ident)
    cfg = config(sessions, ident)
    assert cfg[migration.MARK] == migration.VERSION
    assert state.GENESIS not in cfg
    assert not (root / "journal").exists()
    # The untouched old template becomes the new one; nothing is moved to legacy.
    text = (root / "MEMORY.md").read_text()
    assert layout.PINNED_HEADING in text and "memory/YYYY-MM-DD.md" not in text
    with sessions() as db:
        # Only baselines and the template swap: every injected file has a version to check against.
        assert {v.path for v in versions.listing(db, ident)} == {layout.MEMORY, layout.INDEX}
        assert versions.latest(db, ident, layout.MEMORY).after_text == text


def test_missing_volume_and_unknown_agent_are_quiet(sessions, monkeypatch, tmp_path):
    assert migration.migrate("pa_missing") is False
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    assert migration.migrate("pa_missing") is False
    (tmp_path / "pa_missing" / "workspace").mkdir(parents=True)
    assert migration.migrate("pa_missing") is False


def test_old_workspace_moves_journals_and_splits_memory(workspace, sessions):
    ident, root = workspace
    old_day, new_day = recent(30), recent(2)
    (root / "memory").mkdir()
    (root / "memory" / (old_day + ".md")).write_text("# " + old_day + "\n" + entry("m1", "第一天")[1])
    (root / "memory" / (new_day + ".md")).write_text("# " + new_day + "\n今天和张三开会，自己记的。\n" + entry("m2", "第二天")[1])
    (root / "memory" / "notes.md").write_text("not a journal\n")
    quote = "记住：周五下午不开会"
    (root / "MEMORY.md").write_text("# MEMORY.md - 长期记忆\n\n## 项目\n- Q4 预算 182 万\n\n" + managed("no-friday", quote) + "\n\n随手记一句。\n")
    assert migration.migrate(ident) is True

    assert not (root / "memory" / (old_day + ".md")).exists()
    assert (root / "journal" / (old_day + ".md")).read_text().count("pa-journal:") == 1
    assert "自己记的" in (root / "journal" / (new_day + ".md")).read_text()
    assert (root / "memory" / "notes.md").exists()

    memory = (root / "MEMORY.md").read_text()
    assert memory.startswith("# MEMORY.md")
    assert layout.PINNED_HEADING + "\n- " + quote + "\n" in memory
    assert "memory/_index.md" in memory and "dream-key" not in memory and "Q4" not in memory
    legacy = (root / "journal" / "legacy-memory.md").read_text()
    assert "## 项目\n- Q4 预算 182 万" in legacy and "随手记一句。" in legacy and quote not in legacy

    cfg = config(sessions, ident)
    assert cfg[migration.MARK] == migration.VERSION
    genesis = cfg[state.GENESIS]
    assert genesis["status"] == "requested"
    assert datetime.fromisoformat(genesis["since"]).date().isoformat() == old_day
    with sessions() as db:
        rows = versions.listing(db, ident)
    paths = {(r.path, r.writer, r.after_text is None) for r in rows}
    assert (layout.MEMORY, layout.SYSTEM, False) in paths
    # Journal moves are history, logged but not versioned (they are not memory files).
    assert not [r for r in rows if r.path.startswith(("journal/", "memory/" + old_day))]


def test_rerun_is_a_noop(workspace, sessions):
    ident, root = workspace
    (root / "memory").mkdir()
    (root / "memory" / (recent(3) + ".md")).write_text("# x\n" + entry("m1", "a")[1])
    assert migration.migrate(ident) is True
    with sessions() as db:
        count = len(versions.listing(db, ident))
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert migration.migrate(ident) is False
    seed.seed(ident)
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and not p.name.startswith("_")}
    assert {p: b for p, b in before.items() if not p.name.startswith("_")} == after
    with sessions() as db:
        assert len(versions.listing(db, ident)) == count


def test_genesis_since_is_capped_at_replay_window(workspace, sessions):
    ident, root = workspace
    (root / "memory").mkdir()
    (root / "memory" / "2020-01-01.md").write_text("# 2020-01-01\n" + entry("m1", "很久以前")[1])
    migration.migrate(ident)
    since = datetime.fromisoformat(config(sessions, ident)[state.GENESIS]["since"])
    assert abs((datetime.now(since.tzinfo) - since) - timedelta(days=migration.REPLAY_DAYS)) < timedelta(minutes=5)


def test_day_already_in_journal_is_merged_without_duplicates(workspace, sessions):
    ident, root = workspace
    day = recent(1)
    (root / "memory").mkdir()
    (root / "journal").mkdir()
    shared_marker, shared = entry("m1", "两边都有")
    only_old_marker, only_old = entry("m2", "只在旧文件")
    (root / "journal" / (day + ".md")).write_text("# " + day + "\n" + shared + entry("m3", "新写入器")[1])
    (root / "memory" / (day + ".md")).write_text("# " + day + "\n" + shared + only_old)
    migration.migrate(ident)
    merged = (root / "journal" / (day + ".md")).read_text()
    assert merged.count(shared_marker) == 1 and merged.count(only_old_marker) == 1
    assert merged.count("pa-journal:") == 3 and merged.count("# " + day) == 1
    assert not (root / "memory" / (day + ".md")).exists()


def test_symlink_in_memory_is_left_alone(workspace, sessions, tmp_path):
    ident, root = workspace
    secret = tmp_path / "outside.md"
    secret.write_text("outside the workspace\n")
    (root / "memory").mkdir()
    os.symlink(secret, root / "memory" / (recent(4) + ".md"))
    (root / "memory" / (recent(5) + ".md")).write_text("# d\n" + entry("m1", "real")[1])
    migration.migrate(ident)
    assert (root / "memory" / (recent(4) + ".md")).is_symlink()
    assert not (root / "journal" / (recent(4) + ".md")).exists()
    assert (root / "journal" / (recent(5) + ".md")).exists()
    assert secret.read_text() == "outside the workspace\n"


def test_memory_dir_link_and_journal_link_do_not_escape(workspace, sessions, tmp_path):
    ident, root = workspace
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / (recent(1) + ".md")).write_text("# d\n")
    os.symlink(outside, root / "memory")
    assert migration.migrate(ident) is False
    assert (outside / (recent(1) + ".md")).exists()
    assert config(sessions, ident)[migration.MARK] == migration.VERSION


def test_held_lock_defers_without_marking(workspace, sessions, monkeypatch):
    import threading
    ident, root = workspace
    (root / "memory").mkdir()
    (root / "memory" / (recent(1) + ".md")).write_text("# d\n")
    monkeypatch.setattr(_pa_files, "LOCK_WAIT_SECONDS", 0.1)
    holding, done = threading.Event(), threading.Event()

    def hold():   # another writer: the lock is re-entrant only within one thread
        with _pa_files.workspace_lock(ident):
            holding.set()
            done.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(5)
    try:
        assert migration.migrate(ident) is False
    finally:
        done.set()
        thread.join()
    assert migration.MARK not in config(sessions, ident)
    assert migration.migrate(ident) is True


def test_merge_keeps_handwritten_head():
    marker, block = entry("m1", "x")
    merged = migration.merge("# 2026-11-01\n" + block, "# 2026-11-01\n手写的一段\n" + block, "2026-11-01")
    assert merged.count(marker) == 1 and "手写的一段" in merged


OLD_AGENTS = (Path(__file__).parent / "old_agents_template.md").read_text(encoding="utf-8")


def test_untouched_old_agents_becomes_current_template(workspace, sessions):
    from personal_agent.workspace.prompting import prompt_part
    ident, root = workspace
    assert hashlib.sha256(OLD_AGENTS.strip().encode()).hexdigest() in migration.OLD_AGENTS
    (root / "AGENTS.md").write_text(OLD_AGENTS.rstrip("\n") + migration.OLD_NOTICE)
    seed.seed(ident, config(sessions, ident))
    text = (root / "AGENTS.md").read_text()
    assert text == prompt_part("template_agents") + "\n"
    assert "memory/YYYY-MM-DD.md" not in text and "今天和昨天" not in text


def test_edited_agents_loses_only_the_old_passages(workspace, sessions):
    ident, root = workspace
    own = "\n## 我自己加的\n\n周五提醒周报。\n"
    (root / "AGENTS.md").write_text(OLD_AGENTS.rstrip("\n") + own + migration.OLD_NOTICE)
    seed.seed(ident, config(sessions, ident))
    text = (root / "AGENTS.md").read_text()
    assert "memory/YYYY-MM-DD.md" not in text and "今天和昨天" not in text
    assert "周五提醒周报。" in text and "journal/YYYY-MM-DD.md" in text and "## 记忆（平台已更新）" not in text


def test_current_agents_is_left_alone(workspace, sessions):
    ident, root = workspace
    (root / "AGENTS.md").write_text("# AGENTS.md\n\n自己的约定。\n")
    before = (root / "AGENTS.md").stat().st_mtime_ns
    assert migration.refresh_agents(ident) == []
    assert (root / "AGENTS.md").stat().st_mtime_ns == before
