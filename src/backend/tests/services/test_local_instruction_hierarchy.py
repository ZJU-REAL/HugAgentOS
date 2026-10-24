"""Local-project AGENTS.md hierarchy (CLI contract §8.2): global, git root ... project."""

from __future__ import annotations

import os

import pytest

from core.projects.instructions import inherited_local_instructions
from prompts import prompt_runtime  # first: resolves the prompt_runtime <-> project_section cycle
from prompts import project_section
from prompts.project_section import _build_local_project_section


@pytest.fixture
def layout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = tmp_path / "hugagent-home"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HUGAGENT_HOME", str(data))
    return home, data


def _write(directory, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "AGENTS.md").write_text(text, encoding="utf-8")


def _paths(entries):
    return [os.path.dirname(path) for path, _ in entries]


def test_order_is_global_then_git_root_down_and_excludes_project_root(layout):
    home, data = layout
    repo = home / "code" / "repo"
    project = repo / "pkg" / "app"
    project.mkdir(parents=True)
    (repo / ".git").mkdir()
    _write(data, "global")
    _write(home, "home rules are above the git root")
    _write(home / "code", "outside the repo")
    _write(repo, "repo")
    _write(repo / "pkg", "pkg")
    _write(project, "project root stays with the instructions service")

    entries = inherited_local_instructions(str(project))
    assert [text for _, text in entries] == ["global", "repo", "pkg"]
    assert _paths(entries) == [str(data), str(repo.resolve()), str((repo / "pkg").resolve())]


def test_git_worktree_file_marks_the_root(layout):
    home, _ = layout
    repo = home / "wt"
    project = repo / "src"
    project.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere\n")
    _write(home, "home")
    _write(repo, "worktree root")
    assert [text for _, text in inherited_local_instructions(str(project))] == ["worktree root"]


def test_without_git_walks_up_to_home_but_never_above(layout, tmp_path):
    home, _ = layout
    project = home / "notes" / "2026"
    project.mkdir(parents=True)
    _write(tmp_path, "above home must never be read")
    _write(home, "home")
    _write(home / "notes", "notes")
    entries = inherited_local_instructions(str(project))
    assert [text for _, text in entries] == ["home", "notes"]


def test_git_root_above_home_is_ignored(layout, tmp_path):
    home, _ = layout
    (tmp_path / ".git").mkdir()
    _write(tmp_path, "above home")
    project = home / "p"
    project.mkdir()
    _write(home, "home")
    assert [text for _, text in inherited_local_instructions(str(project))] == ["home"]


def test_outside_home_without_git_reads_only_global(layout, tmp_path):
    _, data = layout
    project = tmp_path / "srv" / "proj"
    project.mkdir(parents=True)
    _write(tmp_path / "srv", "no bound, not read")
    _write(data, "global")
    assert [text for _, text in inherited_local_instructions(str(project))] == ["global"]


def test_project_at_git_root_or_home_has_no_ancestors(layout):
    home, _ = layout
    _write(home, "home is the project itself")
    assert inherited_local_instructions(str(home)) == []
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(repo, "repo root is the project itself")
    assert inherited_local_instructions(str(repo)) == []


def test_symlinked_oversized_invalid_and_unreadable_files_are_skipped(layout, tmp_path):
    home, data = layout
    project = home / "a" / "b" / "c" / "d" / "e"
    project.mkdir(parents=True)
    (tmp_path / "target.md").write_text("via symlink")
    (data / "AGENTS.md").symlink_to(tmp_path / "target.md")
    (home / "a" / "AGENTS.md").write_bytes(b"x" * (32 * 1024 + 1))
    (home / "a" / "b" / "AGENTS.md").write_bytes(b"\xff\xfe bad utf-8")
    (home / "a" / "b" / "c" / "AGENTS.md").mkdir()
    _write(home / "a" / "b" / "c" / "d", "kept")
    unreadable = home / "AGENTS.md"
    unreadable.write_text("secret")
    unreadable.chmod(0)
    try:
        expected = ["kept"] if not os.access(unreadable, os.R_OK) else ["secret", "kept"]
        assert [text for _, text in inherited_local_instructions(str(project))] == expected
    finally:
        unreadable.chmod(0o600)


def test_invalid_local_paths_yield_nothing(layout, tmp_path):
    assert inherited_local_instructions("") == []
    assert inherited_local_instructions("relative") == []
    assert inherited_local_instructions(str(tmp_path / "missing")) == []


def test_section_labels_each_file_with_root_last():
    section = _build_local_project_section(
        project_name="P",
        project_instructions="root rules",
        local_path="/home/u/repo/app",
        local_slug="p-1",
        inherited_instructions=[("/home/u/.hugagent/AGENTS.md", "global"), ("/home/u/repo/AGENTS.md", "repo")],
    )
    order = [section.index(marker) for marker in (
        "`/home/u/.hugagent/AGENTS.md`\nglobal",
        "`/home/u/repo/AGENTS.md`\nrepo",
        "`/home/u/repo/app/AGENTS.md`\nroot rules",
    )]
    assert order == sorted(order)


def test_section_without_inherited_files_is_unchanged():
    kwargs = dict(project_name="P", project_instructions="root", local_path="/p", local_slug="p")
    assert _build_local_project_section(**kwargs) == _build_local_project_section(
        **kwargs, inherited_instructions=[]
    )
    assert "`/p/AGENTS.md`" not in _build_local_project_section(**kwargs)


@pytest.fixture
def plain_prompt(monkeypatch):
    monkeypatch.setattr(prompt_runtime, "_load_db_prompt_parts", lambda: {})
    monkeypatch.setattr(prompt_runtime, "_get_db_prompt_version", lambda: "test")
    monkeypatch.setattr(project_section, "_load_db_prompt_parts", lambda: {})
    prompt_runtime.invalidate_prompt_cache()
    yield
    prompt_runtime.invalidate_prompt_cache()


def _build(project, *, local):
    from prompts.prompt_config import PromptConfig, SystemPromptConfig

    config = PromptConfig(system_prompt=SystemPromptConfig(provider="inline", inline_template="BASE"))
    return prompt_runtime.build_system_prompt(config, ctx={
        "project_id": "prj_1", "project_name": "P", "project_instructions": "root rules",
        "project_is_local": local, "project_kind": "local" if local else "personal",
        "project_local_path": str(project), "project_local_slug": "p-1", "now": "2026-10-24",
    })


def test_system_prompt_injects_hierarchy_and_busts_cache(layout, plain_prompt, monkeypatch):
    home, data = layout
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    project = home / "repo" / "app"
    project.mkdir(parents=True)
    (home / "repo" / ".git").mkdir()
    _write(data, "GLOBAL-MARKER")
    _write(home / "repo", "REPO-MARKER")
    first = _build(project, local=True)
    assert first.index("GLOBAL-MARKER") < first.index("REPO-MARKER") < first.index("root rules")
    _write(home / "repo", "REPO-EDITED")
    assert "REPO-EDITED" in _build(project, local=True)


def test_cloud_prompt_never_reads_host_files(layout, plain_prompt, monkeypatch):
    home, data = layout
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    _write(data, "GLOBAL-MARKER")
    project = home / "p"
    project.mkdir()
    called = []
    monkeypatch.setattr(
        "core.projects.instructions.inherited_local_instructions", lambda path: called.append(path) or []
    )
    prompt = _build(project, local=False)
    assert "GLOBAL-MARKER" not in prompt and called == []
