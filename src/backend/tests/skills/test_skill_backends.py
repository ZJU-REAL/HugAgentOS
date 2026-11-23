"""FilesystemBackend and CompositeBackend skill discovery and priority merging."""

from __future__ import annotations

from pathlib import Path

from core.capabilities.skills.backends import CompositeBackend, FilesystemBackend
from extensions.paths import SKILLS_DIR

BUILTIN_DIR = SKILLS_DIR / "default"


def _write_skill(root: Path, skill_id: str, description: str, version: str = "1.0.0") -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {description}\nversion: {version}\n---\n"
        f"## Instructions\n- {description} instruction\n",
        encoding="utf-8",
    )


def _builtin_backend() -> FilesystemBackend:
    return FilesystemBackend(root_dir=BUILTIN_DIR, source_name="test-builtin", priority=0)


def test_filesystem_backend_lists_builtin_skills():
    skill_files = _builtin_backend().list_skill_files()

    skill_ids = {sf.skill_id for sf in skill_files}
    assert {"capability-guide-brief", "officecli-docx"} <= skill_ids
    for info in skill_files:
        assert info.source_name == "test-builtin"
        assert info.priority == 0
        assert info.file_path.name == "SKILL.md"


def test_filesystem_backend_reads_builtin_skill_file():
    content = _builtin_backend().read_skill_file("capability-guide-brief")
    assert content.startswith("---")
    assert "name:" in content or "description:" in content


def test_filesystem_backend_exists():
    backend = _builtin_backend()
    assert backend.exists("capability-guide-brief")
    assert not backend.exists("nonexistent-skill-xyz")


def test_filesystem_backend_tolerates_missing_root(tmp_path):
    backend = FilesystemBackend(root_dir=tmp_path / "missing", source_name="test-missing")
    assert backend.list_skill_files() == []
    assert not backend.exists("any-skill")


def test_composite_backend_higher_priority_wins(tmp_path):
    _write_skill(tmp_path / "builtin", "test-skill", "Built-in version")
    _write_skill(tmp_path / "user", "test-skill", "User version", "2.0.0")
    composite = CompositeBackend(
        [
            FilesystemBackend(tmp_path / "builtin", "built-in", priority=0),
            FilesystemBackend(tmp_path / "user", "user", priority=50),
        ]
    )

    skill_files = composite.list_skill_files()
    assert [(sf.skill_id, sf.source_name, sf.priority) for sf in skill_files] == [
        ("test-skill", "user", 50)
    ]
    content = composite.read_skill_file("test-skill")
    assert "User version" in content
    assert "Built-in version" not in content


def test_composite_backend_merges_disjoint_sources(tmp_path):
    _write_skill(tmp_path / "source1", "skill-a", "Skill A")
    _write_skill(tmp_path / "source2", "skill-b", "Skill B")
    composite = CompositeBackend(
        [
            FilesystemBackend(tmp_path / "source1", "source1", priority=10),
            FilesystemBackend(tmp_path / "source2", "source2", priority=20),
        ]
    )

    assert {sf.skill_id for sf in composite.list_skill_files()} == {"skill-a", "skill-b"}
    assert "Skill A" in composite.read_skill_file("skill-a")
    assert "Skill B" in composite.read_skill_file("skill-b")
