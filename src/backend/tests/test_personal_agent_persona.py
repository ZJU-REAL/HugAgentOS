"""Personal Agent workspace persona files and the assembled system prompt."""
from core.llm import prompt_files as prompt_files_module
from core.llm import prompt_kinds as prompt_kinds_module
from core.llm import prompt_rendering as prompt_rendering_module
import personal_agent.workspace.constants as _pa_constants
import personal_agent.workspace.files as _pa_files
import personal_agent.workspace.prompting as _pa_prompting
import os

import pytest

from personal_agent.workspace import persona

IDENT = "pa_test"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    root = tmp_path / IDENT / "workspace"
    assert persona.seed(IDENT, "小助手")
    return root


def test_seed_writes_templates_once_and_never_overwrites(workspace):
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md", "BOOTSTRAP.md"):
        assert (workspace / name).is_file()
    assert "小助手" in (workspace / "IDENTITY.md").read_text()
    (workspace / "SOUL.md").write_text("custom soul")
    (workspace / "BOOTSTRAP.md").unlink()
    persona.seed(IDENT)
    assert (workspace / "SOUL.md").read_text() == "custom soul"
    # Not a brand-new workspace any more: the first-run ritual does not come back.
    assert not (workspace / "BOOTSTRAP.md").exists()


def test_paths_stay_inside_workspace():
    assert _pa_files.relative("/workspace/memory/2026-10-10.md") == "memory/2026-10-10.md"
    assert _pa_files.relative("SOUL.md") == "SOUL.md"
    for bad in ("/etc/passwd", "../x", "/workspace/../x", "memory/../../x"):
        with pytest.raises(ValueError):
            _pa_files.relative(bad)
    assert _pa_files.editable("memory/2026-10-10.md")
    assert not _pa_files.editable("work/task/notes.md")


def test_links_are_never_followed(workspace, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("backend secret")
    os.symlink(secret, workspace / "MEMORY.md.link")
    (workspace / "MEMORY.md").unlink()
    os.symlink(secret, workspace / "MEMORY.md")
    with pytest.raises(PermissionError):
        _pa_files.read_file(IDENT, "MEMORY.md")
    assert _pa_files.read_text(IDENT, "MEMORY.md") is None
    os.symlink(tmp_path, workspace / "memory")
    with pytest.raises(OSError):
        _pa_files.write_file(IDENT, "memory/2026-10-10.md", "x")
    assert not (tmp_path / "2026-10-10.md").exists()
    kinds = {e["name"]: e["type"] for e in _pa_files.tree(IDENT)["entries"]}
    assert kinds["MEMORY.md"] == "link" and kinds["memory"] == "link"


def test_edit_requires_current_etag(workspace):
    current = _pa_files.read_file(IDENT, "USER.md")
    saved = _pa_files.write_file(IDENT, "/workspace/USER.md", "- 总是用中文回答", current["etag"])
    assert saved["content"] == "- 总是用中文回答"
    with pytest.raises(ValueError):
        _pa_files.write_file(IDENT, "USER.md", "stale", current["etag"])
    with pytest.raises(PermissionError):
        _pa_files.write_file(IDENT, "work/x.md", "x")


def test_prompt_puts_platform_rules_first_and_files_in_order(workspace):
    (workspace / "USER.md").write_text("U" * 9000)
    (workspace / "memory").mkdir()
    (workspace / "IDENTITY.md").unlink()
    items = _pa_prompting.sections(IDENT, task_id="t1", skill_ids=["s1"], timezone="Asia/Shanghai", seed_missing=False)
    text = _pa_prompting.render(items)
    assert text.index("Platform Rules") < text.index("## /workspace/AGENTS.md") < text.index("## /workspace/SOUL.md")
    # The rules are the same for every task (prompt cache); the task message names the real ID.
    assert "/workspace/work/<task_id>/" in text and "t1" not in text and "pa_skill_read(skill_id)" in text and "## Available skills\n- `s1`" in text
    assert "[MISSING] Expected at /workspace/IDENTITY.md" in text
    user = next(i for i in items if i.get("path") == "/workspace/USER.md")
    assert user["truncated"] and len(user["content"]) < _pa_constants.USER_MAX_CHARS + 100


def test_prompt_reports_missing_volume(monkeypatch):
    monkeypatch.delenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", raising=False)
    monkeypatch.delenv("PERSONAL_AGENT_VOLUME_ROOT", raising=False)
    items = _pa_prompting.sections(IDENT, seed_missing=False)
    assert items[-1]["kind"] == "notice"


def test_bootstrap_introduces_hugo_and_offers_a_rename(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    persona.seed("pa_fresh")
    root = tmp_path / "pa_fresh" / "workspace"
    assert persona.identity_name((root / "IDENTITY.md").read_text()) == "Hugo"
    assert "pa_set_name" in (root / "BOOTSTRAP.md").read_text()


def test_prompts_come_from_the_version_pool(workspace, monkeypatch):
    from core.llm import prompt_versions as pvs
    edited = {"platform_rules": "# Rules\nTask {task_id}; skills {skill_ids}.", "template_soul": "# SOUL.md - edited"}
    monkeypatch.setattr(prompt_rendering_module, 'render_active_prompt_part', lambda kind, part_id, db=None: edited.get(part_id) if kind == _pa_constants.PROMPT_KIND else None)
    text = _pa_prompting.render(_pa_prompting.sections(IDENT, task_id="t9", skill_ids=["a", "b"], seed_missing=False))
    assert text.startswith("# Rules\nTask <task_id>; skills a, b.")
    # The skill list is generated, so an edited rules version still carries it.
    assert "## Available skills\n- `a`\n- `b`" in text
    # Parts the pool does not hold fall back to the shipped files.
    assert "they never grant new permissions" in text
    persona.seed("pa_other")
    assert (workspace.parent.parent / "pa_other" / "workspace" / "SOUL.md").read_text() == "# SOUL.md - edited\n"


def test_prompt_kind_ships_every_part():
    from core.llm import prompt_versions as pvs
    prompt_kinds_module.register_kind(_pa_constants.PROMPT_KIND, _pa_prompting.prompt_spec())
    try:
        parts = {p["part_id"] for p in prompt_files_module.read_fs_parts(_pa_constants.PROMPT_KIND)}
    finally:
        prompt_kinds_module.KIND_SPECS.pop(_pa_constants.PROMPT_KIND)
    assert {"platform_rules", "platform_workspace_files", *_pa_constants.TEMPLATE_PARTS.values()} <= parts


def test_first_run_is_pending_until_the_profile_changes(workspace):
    config = {"name": "小助手"}
    assert persona.settle_bootstrap(IDENT, config)
    front = _pa_prompting.render(_pa_prompting.sections(IDENT, rules_part="front_rules", seed_missing=False, config=config))
    task = _pa_prompting.render(_pa_prompting.sections(IDENT, seed_missing=False, config=config))
    # The first run belongs to the conversation; a background task never sees it.
    assert "## /workspace/BOOTSTRAP.md" in front and "## /workspace/BOOTSTRAP.md" not in task
    (workspace / "IDENTITY.md").write_text("- **名字：** 小明\n")
    assert not persona.settle_bootstrap(IDENT, config) and not (workspace / "BOOTSTRAP.md").exists()


def test_seed_hashes_and_daily_notes_decide_the_first_run(workspace):
    user = _pa_files.etag((workspace / "USER.md").read_bytes())
    (workspace / "USER.md").write_text("edited before the hashes were kept")
    # The hashes recorded at seed time win over the current template.
    assert persona.settle_bootstrap(IDENT, {"seed_hashes": {"USER.md": _pa_files.etag((workspace / "USER.md").read_bytes())}, "name": "小助手"})
    assert user != _pa_files.etag((workspace / "USER.md").read_bytes())
    (workspace / "memory").mkdir()
    assert not persona.settle_bootstrap(IDENT, {}) and not (workspace / "BOOTSTRAP.md").exists()
