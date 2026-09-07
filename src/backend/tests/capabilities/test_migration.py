"""旧布局迁移：拷贝隔离、旧链接清除、会话拷贝清理、散放技能导入；幂等。"""

from __future__ import annotations

import os

from core.agent_skills import config as skill_config
from core.capabilities import junction, migration, registry, store
from core.capabilities.paths import KIND_SKILL


def _md(sid: str, body: str = "x") -> str:
    return f"---\nname: {sid}\ndescription: d\n---\n{body}\n"


def test_migration_moves_legacy_layout_aside_and_imports_flat_skills(
    tmp_path, monkeypatch, caps_root, index_db
):
    ws = tmp_path / "ws"
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(ws / "skills"))
    shared = ws / "skills"
    (shared / "ppt-design").mkdir(parents=True)
    (shared / "ppt-design" / "SKILL.md").write_text(_md("ppt-design"))
    users = ws / "skills_u"
    (users / "alice" / "mine").mkdir(parents=True)
    (users / "alice" / "mine" / "SKILL.md").write_text(_md("mine"))
    os.symlink("../skills", users / "skills_shared", target_is_directory=True)
    os.symlink(
        "../skills_shared/ppt-design", users / "alice" / "ppt-design", target_is_directory=True
    )
    (ws / "skills_cloud" / "market-x").mkdir(parents=True)
    (ws / "skills_cloud" / "market-x" / "SKILL.md").write_text(_md("market-x"))
    session = ws / ".sessions" / "abc"
    (session / "skills" / "ppt-design").mkdir(parents=True)
    (session / "skills" / "ppt-design" / "SKILL.md").write_text(_md("ppt-design"))
    dropped = caps_root / "skills" / "hand-made"
    dropped.mkdir(parents=True)
    (dropped / "SKILL.md").write_text(_md("hand-made", "by hand"))
    (dropped / "scripts").mkdir()
    (dropped / "scripts" / "run.py").write_text("print(1)")

    report = migration.migrate_legacy_layout()
    assert report is not None and report.changed
    assert not (shared / "ppt-design").exists()
    assert not (users / "alice" / "mine").exists() and not (users / "skills_shared").exists()
    assert not (users / "alice" / "ppt-design").exists()
    assert not (ws / "skills_cloud").exists()
    assert not (session / "skills").exists()
    assert report.imported == ["hand-made"] and not dropped.exists()
    q = report.quarantine_dir
    assert q and (caps_root / ".capabilities" / "migrations").exists()
    assert (
        tmp_path
        / q.split(str(tmp_path))[1].lstrip("/")
        / "skills_cloud"
        / "skills_cloud"
        / "market-x"
        / "SKILL.md"
    ).exists()

    inst = registry.get(registry.install_id(KIND_SKILL, "local", "hand-made"))
    assert inst.ready
    comp = store.get(KIND_SKILL, "local", "hand-made", inst.resolved_revision)
    assert (comp.path / "scripts" / "run.py").read_text() == "print(1)"

    # second run: nothing left to do
    again = migration.migrate_legacy_layout()
    assert not again.changed and not again.skipped
