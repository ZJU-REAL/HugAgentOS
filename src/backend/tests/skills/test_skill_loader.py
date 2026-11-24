"""Skill source configuration and MultiSourceSkillLoader metadata/full-spec loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.capabilities.skills import loader_factory
from core.capabilities.skills.backends import CompositeBackend, FilesystemBackend
from core.capabilities.skills.config import get_default_skill_sources
from core.capabilities.skills.loader import MultiSourceSkillLoader
from core.capabilities.skills.loader_factory import get_skill_loader
from extensions.paths import SKILLS_DIR

BUILTIN_DIR = SKILLS_DIR / "default"


def _builtin_composite() -> CompositeBackend:
    return CompositeBackend([FilesystemBackend(BUILTIN_DIR, "built-in", priority=0)])


@pytest.fixture
def builtin_loader() -> MultiSourceSkillLoader:
    # Only the shipped skills folder: the default backend also reads admin skills from the DB.
    return MultiSourceSkillLoader(backend=_builtin_composite())


def _write_skill(root: Path, skill_id: str, description: str, version: str) -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {description}\nversion: {version}\n---\n"
        "## Instructions\n- Do the thing\n",
        encoding="utf-8",
    )


def test_default_sources_and_priorities():
    sources = {src.name: src for src in get_default_skill_sources()}

    assert list(sources) == ["built-in", "admin", "user", "project"]
    assert {name: src.priority for name, src in sources.items()} == {
        "built-in": 0,
        "admin": 75,
        "user": 50,
        "project": 100,
    }
    assert sources["built-in"].root_dir == BUILTIN_DIR.resolve()
    assert sources["built-in"].enabled is True


def test_skill_source_directories_follow_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("HUGAGENT_USER_SKILLS_DIR", str(tmp_path / "user"))
    monkeypatch.setenv("HUGAGENT_PROJECT_SKILLS_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("HUGAGENT_ADMIN_SKILLS_DIR", str(tmp_path / "admin"))
    monkeypatch.setenv("HUGAGENT_DISABLE_PROJECT_SKILLS", "1")

    sources = {src.name: src for src in get_default_skill_sources()}

    assert sources["user"].root_dir == (tmp_path / "user").resolve()
    assert sources["project"].root_dir == (tmp_path / "project").resolve()
    assert sources["admin"].root_dir == (tmp_path / "admin").resolve()
    assert sources["project"].enabled is False


def test_loader_reads_builtin_metadata(builtin_loader):
    metadata_map = builtin_loader.load_all_metadata()

    assert {"capability-guide-brief", "officecli-docx"} <= set(metadata_map)
    guide = metadata_map["capability-guide-brief"]
    assert guide.id == "capability-guide-brief"
    assert guide.name and guide.description
    assert guide.skill_path.startswith("built-in:")
    # Metadata is the lightweight phase: instructions are only parsed on demand.
    assert not hasattr(guide, "instructions")


def test_loader_caches_metadata(builtin_loader):
    assert builtin_loader.load_all_metadata() is builtin_loader.load_all_metadata()


def test_every_builtin_skill_loads_a_full_spec(builtin_loader):
    metadata_map = builtin_loader.load_all_metadata()
    assert metadata_map

    for skill_id in metadata_map:
        spec = builtin_loader.load_skill_full(skill_id)
        assert spec is not None, skill_id
        assert spec.id == skill_id
        assert spec.instructions, skill_id
        assert spec.skill_path.startswith("built-in:")


def test_load_skill_full_returns_none_for_unknown_skill(builtin_loader):
    assert builtin_loader.load_skill_full("nonexistent-skill-xyz") is None


def test_higher_priority_source_overrides_skill(tmp_path):
    _write_skill(tmp_path / "builtin", "test-skill", "Built-in version", "1.0.0")
    _write_skill(tmp_path / "project", "test-skill", "Project version", "2.0.0")
    loader = MultiSourceSkillLoader(
        backend=CompositeBackend(
            [
                FilesystemBackend(tmp_path / "builtin", "built-in", priority=0),
                FilesystemBackend(tmp_path / "project", "project", priority=100),
            ]
        )
    )

    meta = loader.load_all_metadata()["test-skill"]
    assert meta.description == "Project version"
    assert meta.skill_path.startswith("project:")

    spec = loader.load_skill_full("test-skill")
    assert spec is not None
    assert spec.description == "Project version"
    assert spec.version == "2.0.0"
    assert spec.skill_path.startswith("project:")


def test_get_skill_source(builtin_loader):
    assert builtin_loader.get_skill_source("capability-guide-brief") == "built-in"
    assert builtin_loader.get_skill_source("nonexistent-xyz") is None


def test_global_loader_is_a_resettable_singleton(monkeypatch):
    monkeypatch.setattr(MultiSourceSkillLoader, "_create_default_backend", staticmethod(_builtin_composite))
    monkeypatch.setattr(loader_factory, "global_loader", None)

    first = get_skill_loader()
    assert get_skill_loader() is first
    assert get_skill_loader(reset=True) is not first
