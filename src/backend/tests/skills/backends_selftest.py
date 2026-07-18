"""Selftest for skill backends."""

from __future__ import annotations

import tempfile
from pathlib import Path

from core.agent_skills.backends import (
    CompositeBackend,
    FilesystemBackend,
    SkillFileInfo,
)


def test_filesystem_backend_list():
    """Test FilesystemBackend.list_skill_files()."""
    # Use existing built-in skills directory
    builtin_dir = Path(__file__).parent.parent.parent / "skill_bundles"
    backend = FilesystemBackend(
        root_dir=builtin_dir,
        source_name="test-builtin",
        priority=0,
    )

    skill_files = backend.list_skill_files()
    assert skill_files, "Expected at least some built-in skills"

    skill_ids = {sf.skill_id for sf in skill_files}
    assert "capability-guide-brief" in skill_ids
    assert "quick-material-analysis" in skill_ids

    # Check SkillFileInfo fields
    first_skill = skill_files[0]
    assert first_skill.source_name == "test-builtin"
    assert first_skill.priority == 0
    assert first_skill.file_path.name == "SKILL.md"

    print(f"✓ FilesystemBackend.list_skill_files() found {len(skill_files)} skills")


def test_filesystem_backend_read():
    """Test FilesystemBackend.read_skill_file()."""
    builtin_dir = Path(__file__).parent.parent.parent / "skill_bundles"
    backend = FilesystemBackend(
        root_dir=builtin_dir,
        source_name="test-builtin",
        priority=0,
    )

    # Read a known skill
    content = backend.read_skill_file("capability-guide-brief")
    assert content, "Expected non-empty content"
    assert "---\n" in content  # YAML frontmatter
    assert "name:" in content or "description:" in content

    print("✓ FilesystemBackend.read_skill_file() works")


def test_filesystem_backend_exists():
    """Test FilesystemBackend.exists()."""
    builtin_dir = Path(__file__).parent.parent.parent / "skill_bundles"
    backend = FilesystemBackend(
        root_dir=builtin_dir,
        source_name="test-builtin",
        priority=0,
    )

    assert backend.exists("capability-guide-brief")
    assert not backend.exists("nonexistent-skill-xyz")

    print("✓ FilesystemBackend.exists() works")


def test_filesystem_backend_nonexistent_dir():
    """Test FilesystemBackend with nonexistent directory."""
    backend = FilesystemBackend(
        root_dir="/tmp/nonexistent-skills-dir-xyz",
        source_name="test-missing",
        priority=0,
    )

    # Should return empty list, not crash
    skill_files = backend.list_skill_files()
    assert skill_files == []

    assert not backend.exists("any-skill")

    print("✓ FilesystemBackend handles nonexistent directory gracefully")


def test_composite_backend_priority():
    """Test CompositeBackend priority-based merging."""
    # Create temporary skill directories
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Built-in skill
        builtin_dir = tmp_path / "builtin"
        builtin_skill_dir = builtin_dir / "test-skill"
        builtin_skill_dir.mkdir(parents=True)
        (builtin_skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: Built-in version\nversion: 1.0.0\n---\n"
            "## Instructions\n- Built-in instruction\n"
        )

        # User skill (higher priority)
        user_dir = tmp_path / "user"
        user_skill_dir = user_dir / "test-skill"
        user_skill_dir.mkdir(parents=True)
        (user_skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: User version\nversion: 2.0.0\n---\n"
            "## Instructions\n- User instruction\n"
        )

        # Create backends
        builtin_backend = FilesystemBackend(builtin_dir, "built-in", priority=0)
        user_backend = FilesystemBackend(user_dir, "user", priority=50)

        # Composite backend
        composite = CompositeBackend([builtin_backend, user_backend])

        # Should only have one test-skill (user version wins)
        skill_files = composite.list_skill_files()
        assert len(skill_files) == 1
        assert skill_files[0].skill_id == "test-skill"
        assert skill_files[0].source_name == "user"
        assert skill_files[0].priority == 50

        # Read should return user version
        content = composite.read_skill_file("test-skill")
        assert "User version" in content
        assert "Built-in version" not in content

        print("✓ CompositeBackend priority-based merging works")


def test_composite_backend_multiple_sources():
    """Test CompositeBackend with multiple non-overlapping sources."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Source 1: skill-a
        source1_dir = tmp_path / "source1"
        skill_a_dir = source1_dir / "skill-a"
        skill_a_dir.mkdir(parents=True)
        (skill_a_dir / "SKILL.md").write_text(
            "---\nname: skill-a\ndescription: Skill A\nversion: 1.0.0\n---\n"
            "## Instructions\n- Instruction A\n"
        )

        # Source 2: skill-b
        source2_dir = tmp_path / "source2"
        skill_b_dir = source2_dir / "skill-b"
        skill_b_dir.mkdir(parents=True)
        (skill_b_dir / "SKILL.md").write_text(
            "---\nname: skill-b\ndescription: Skill B\nversion: 1.0.0\n---\n"
            "## Instructions\n- Instruction B\n"
        )

        # Create composite
        backend1 = FilesystemBackend(source1_dir, "source1", priority=10)
        backend2 = FilesystemBackend(source2_dir, "source2", priority=20)
        composite = CompositeBackend([backend1, backend2])

        # Should have both skills
        skill_files = composite.list_skill_files()
        skill_ids = {sf.skill_id for sf in skill_files}
        assert skill_ids == {"skill-a", "skill-b"}

        # Both should be readable
        content_a = composite.read_skill_file("skill-a")
        assert "Skill A" in content_a

        content_b = composite.read_skill_file("skill-b")
        assert "Skill B" in content_b

        print("✓ CompositeBackend merges multiple sources correctly")


def main() -> int:
    """Run all backend tests."""
    test_filesystem_backend_list()
    test_filesystem_backend_read()
    test_filesystem_backend_exists()
    test_filesystem_backend_nonexistent_dir()
    test_composite_backend_priority()
    test_composite_backend_multiple_sources()

    print("\nbackends_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
