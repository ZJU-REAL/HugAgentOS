"""Selftest for multi-source skill loader."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from core.agent_skills.backends import CompositeBackend, FilesystemBackend
from core.agent_skills.config import get_default_skill_sources, get_enabled_skill_sources
from core.agent_skills.loader import MultiSourceSkillLoader, get_skill_loader


def test_config_default_sources():
    """Test get_default_skill_sources() returns expected sources."""
    sources = get_default_skill_sources()

    # Should have 3 sources: built-in, user, project
    assert len(sources) == 3

    # Check names and priorities
    names = [src.name for src in sources]
    assert names == ["built-in", "user", "project"]

    priorities = [src.priority for src in sources]
    assert priorities == [0, 50, 100]

    # Built-in should point to skill_bundles/
    builtin = sources[0]
    assert builtin.name == "built-in"
    assert builtin.root_dir.name == "skills"
    assert builtin.enabled is True

    print("✓ get_default_skill_sources() returns correct configuration")


def test_config_env_override():
    """Test environment variable overrides for skill directories."""
    # Save original env
    orig_user = os.environ.get("HUGAGENT_USER_SKILLS_DIR")
    orig_project = os.environ.get("HUGAGENT_PROJECT_SKILLS_DIR")

    try:
        # Set custom paths
        os.environ["HUGAGENT_USER_SKILLS_DIR"] = "/custom/user/skills"
        os.environ["HUGAGENT_PROJECT_SKILLS_DIR"] = "/custom/project/skills"

        sources = get_default_skill_sources()
        user_src = sources[1]
        project_src = sources[2]

        assert str(user_src.root_dir) == "/custom/user/skills"
        assert str(project_src.root_dir) == "/custom/project/skills"

        print("✓ Environment variable overrides work")
    finally:
        # Restore original env
        if orig_user is None:
            os.environ.pop("HUGAGENT_USER_SKILLS_DIR", None)
        else:
            os.environ["HUGAGENT_USER_SKILLS_DIR"] = orig_user

        if orig_project is None:
            os.environ.pop("HUGAGENT_PROJECT_SKILLS_DIR", None)
        else:
            os.environ["HUGAGENT_PROJECT_SKILLS_DIR"] = orig_project


def test_loader_builtin_skills():
    """Test MultiSourceSkillLoader loads built-in skills."""
    loader = MultiSourceSkillLoader()

    # Load all metadata
    metadata_map = loader.load_all_metadata()
    assert metadata_map, "Expected at least some built-in skills"

    skill_ids = set(metadata_map.keys())
    assert "capability-guide-brief" in skill_ids
    assert "quick-material-analysis" in skill_ids

    # Check metadata structure
    guide_meta = metadata_map["capability-guide-brief"]
    assert guide_meta.id == "capability-guide-brief"
    assert guide_meta.name
    assert guide_meta.description
    assert "built-in:" in guide_meta.skill_path  # Source annotation

    print(f"✓ MultiSourceSkillLoader loads {len(metadata_map)} built-in skills")


def test_loader_load_skill_full():
    """Test loading full skill spec on-demand."""
    loader = MultiSourceSkillLoader()

    # Load a known skill
    spec = loader.load_skill_full("capability-guide-brief")
    assert spec is not None
    assert spec.id == "capability-guide-brief"
    assert spec.instructions, "Expected non-empty instructions"
    assert "built-in:" in spec.skill_path

    # Load nonexistent skill
    missing = loader.load_skill_full("nonexistent-skill-xyz")
    assert missing is None

    print("✓ load_skill_full() works correctly")


def test_loader_priority_override():
    """Test that higher priority sources override lower priority."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Create built-in skill
        builtin_dir = tmp_path / "builtin"
        builtin_skill_dir = builtin_dir / "test-skill"
        builtin_skill_dir.mkdir(parents=True)
        (builtin_skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: test-skill\n"
            "description: Built-in version\n"
            "version: 1.0.0\n"
            "---\n"
            "## Instructions\n"
            "- Built-in instruction\n"
        )

        # Create project skill (higher priority)
        project_dir = tmp_path / "project"
        project_skill_dir = project_dir / "test-skill"
        project_skill_dir.mkdir(parents=True)
        (project_skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: test-skill\n"
            "description: Project version (overrides built-in)\n"
            "version: 2.0.0\n"
            "---\n"
            "## Instructions\n"
            "- Project instruction\n"
        )

        # Create loader with custom backends
        builtin_backend = FilesystemBackend(builtin_dir, "built-in", priority=0)
        project_backend = FilesystemBackend(project_dir, "project", priority=100)
        composite = CompositeBackend([builtin_backend, project_backend])
        loader = MultiSourceSkillLoader(backend=composite)

        # Load metadata
        metadata_map = loader.load_all_metadata()
        assert "test-skill" in metadata_map

        # Should use project version (higher priority)
        meta = metadata_map["test-skill"]
        assert "Project version" in meta.description
        assert "project:" in meta.skill_path

        # Load full spec
        spec = loader.load_skill_full("test-skill")
        assert spec is not None
        assert "Project version" in spec.description
        assert spec.version == "2.0.0"
        assert "project:" in spec.skill_path

        print("✓ Priority override works correctly")


def test_loader_get_skill_source():
    """Test get_skill_source() helper method."""
    loader = MultiSourceSkillLoader()

    # Check source for a built-in skill
    source = loader.get_skill_source("capability-guide-brief")
    assert source == "built-in"

    # Check nonexistent skill
    missing_source = loader.get_skill_source("nonexistent-xyz")
    assert missing_source is None

    print("✓ get_skill_source() works correctly")


def test_global_loader_singleton():
    """Test get_skill_loader() returns singleton instance."""
    loader1 = get_skill_loader()
    loader2 = get_skill_loader()

    # Should be the same instance
    assert loader1 is loader2

    # Reset should create new instance
    loader3 = get_skill_loader(reset=True)
    assert loader3 is not loader1

    print("✓ Global loader singleton works correctly")


def main() -> int:
    """Run all loader tests."""
    test_config_default_sources()
    test_config_env_override()
    test_loader_builtin_skills()
    test_loader_load_skill_full()
    test_loader_priority_override()
    test_loader_get_skill_source()
    test_global_loader_singleton()

    print("\nloader_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
