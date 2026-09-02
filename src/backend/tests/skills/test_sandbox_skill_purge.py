"""How skill files reach a sandbox: per-user isolation, and cleanup on delete.

The skill dir mounted at /workspace/skills is the caller's own view — shared
skills plus that user's private ones — so a market skill installed with
credentials (its secrets.json) is never readable from another user's sandbox.
Deleting a skill must take its files with it; nothing used to remove them.
"""
from __future__ import annotations

import pytest

from core.agent_skills.config import (
    SHARED_LINK_NAME,
    get_default_skill_sources,
    get_sandbox_skills_dir,
    get_user_skills_dir,
    prune_orphan_sandbox_skill_dirs,
    purge_skill_sandbox_files,
    skill_files_dir,
    sync_user_skill_view,
)


def _a_builtin_skill_id() -> str:
    """Pick a real built-in bundle name — the set changes as skills retire."""
    root = next(s.root_dir for s in get_default_skill_sources() if s.name == "built-in")
    return sorted(d.name for d in root.iterdir() if (d / "SKILL.md").exists())[0]


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "sandbox_skills"))
    return get_sandbox_skills_dir()


def _make(root, skill_id: str) -> None:
    d = root / skill_id
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("# skill", encoding="utf-8")
    (d / "secrets.json").write_text('{"API_KEY": "x"}', encoding="utf-8")


# ── 隔离 ──────────────────────────────────────────────────────────────────
def test_private_skills_land_in_their_owners_dir(skills_dir) -> None:
    assert skill_files_dir("global-skill").parent == skills_dir
    mine = skill_files_dir("market-skill-6533a8", "alice")
    assert mine.parent == get_user_skills_dir("alice")
    assert mine.parent != skill_files_dir("market-skill-991dc1", "bob").parent


def test_view_shows_shared_skills_plus_only_my_private_ones(skills_dir) -> None:
    _make(skills_dir, "cn-web-search")
    _make(get_user_skills_dir("alice"), "market-skill-alice")
    _make(get_user_skills_dir("bob"), "market-skill-bob")

    view = sync_user_skill_view("alice")

    names = {e.name for e in view.iterdir()}
    assert names == {"cn-web-search", "market-skill-alice"}
    # The shared entry is a link into the shared tree, not a copy.
    assert (view / "cn-web-search").is_symlink()
    mine = view / "market-skill-alice"
    assert mine.is_dir() and not mine.is_symlink()


def test_shared_link_resolves_from_the_view(skills_dir) -> None:
    _make(skills_dir, "cn-web-search")
    view = sync_user_skill_view("alice")

    # Relative, so the same link resolves on the host, inside a sandbox and in
    # the script-runner container.
    link = view / "cn-web-search"
    assert str(link.readlink()) == f"../{SHARED_LINK_NAME}/cn-web-search"
    assert (link / "SKILL.md").read_text(encoding="utf-8") == "# skill"


def test_view_drops_links_of_removed_shared_skills(skills_dir) -> None:
    _make(skills_dir, "retired-skill")
    view = sync_user_skill_view("alice")
    assert (view / "retired-skill").is_symlink()

    purge_skill_sandbox_files("retired-skill")
    sync_user_skill_view("alice")

    assert "retired-skill" not in {e.name for e in view.iterdir()}


def test_no_view_without_a_usable_user_id(skills_dir) -> None:
    # Falls back to the shared-only mount rather than inventing a dir.
    assert sync_user_skill_view(None) is None
    assert sync_user_skill_view("") is None
    assert get_user_skills_dir("..") is None
    assert get_user_skills_dir(SHARED_LINK_NAME) is None


# ── 删除清理 ──────────────────────────────────────────────────────────────
def test_purge_removes_the_files_of_a_private_skill(skills_dir) -> None:
    _make(get_user_skills_dir("alice"), "market-skill-alice")
    assert purge_skill_sandbox_files("market-skill-alice") is True
    assert not (get_user_skills_dir("alice") / "market-skill-alice").exists()


def test_purge_is_a_noop_for_missing_or_unsafe_ids(skills_dir) -> None:
    assert purge_skill_sandbox_files("never-materialized") is False
    assert purge_skill_sandbox_files("") is False
    assert purge_skill_sandbox_files("../etc") is False
    assert purge_skill_sandbox_files("a/b") is False


def test_purge_keeps_builtin_skills(skills_dir) -> None:
    # Built-in dirs are a shared startup copy of the git-tracked bundle, not
    # owned by whatever row was just deleted.
    builtin = _a_builtin_skill_id()
    _make(skills_dir, builtin)
    assert purge_skill_sandbox_files(builtin) is False
    assert (skills_dir / builtin).is_dir()


def test_prune_sweeps_leftovers_in_both_layers(skills_dir) -> None:
    builtin = _a_builtin_skill_id()
    _make(skills_dir, builtin)
    _make(skills_dir, "still-global")
    _make(skills_dir, "deleted-long-ago")
    _make(skills_dir, "private-in-the-wrong-place")  # pre-isolation leftover
    _make(get_user_skills_dir("alice"), "market-skill-alice")
    _make(get_user_skills_dir("alice"), "market-skill-alice-deleted")
    sync_user_skill_view("alice")

    removed = prune_orphan_sandbox_skill_dirs(
        {
            "still-global": None,
            "private-in-the-wrong-place": "alice",
            "market-skill-alice": "alice",
        }
    )

    assert removed == 3
    assert (skills_dir / builtin).is_dir()
    assert (skills_dir / "still-global").is_dir()
    assert not (skills_dir / "deleted-long-ago").exists()
    assert not (skills_dir / "private-in-the-wrong-place").exists()
    assert (get_user_skills_dir("alice") / "market-skill-alice").is_dir()
    assert not (get_user_skills_dir("alice") / "market-skill-alice-deleted").exists()


def test_prune_keeps_the_view_links(skills_dir) -> None:
    _make(skills_dir, "still-global")
    view = sync_user_skill_view("alice")

    prune_orphan_sandbox_skill_dirs({"still-global": None})

    assert (view / "still-global").is_symlink()


def test_loader_materializes_a_private_skill_into_its_owners_dir(skills_dir) -> None:
    """The owner recorded on the row picks the dir, which is what keeps views correct."""
    from pathlib import Path

    from core.agent_skills.backends.composite import CompositeBackend
    from core.agent_skills.backends.protocol import SkillFileInfo
    from core.agent_skills.loader import MultiSourceSkillLoader

    def _info(skill_id: str, owner):
        return SkillFileInfo(
            skill_id=skill_id,
            file_path=Path(f"/db/admin_skills/{skill_id}/SKILL.md"),
            source_name="admin",
            priority=75,
            metadata={
                "id": skill_id,
                "name": skill_id,
                "description": "",
                "version": "1.0.0",
                "tags": [],
                "allowed_tools": [],
                "mcp_server_ids": [],
                "owner_user_id": owner,
            },
            is_database=True,
        )

    class Backend:
        source_name = "admin"
        priority = 75

        def list_skill_files(self):
            return [_info("global-one", None), _info("market-one", "alice")]

        def read_skill_file(self, skill_id):
            return f"---\nname: {skill_id}\ndescription: x\n---\n\n## Instructions\n\nrun\n"

        def get_extra_files(self, skill_id):
            return {"secrets.json": '{"API_KEY": "x"}'}

        def exists(self, skill_id):
            return skill_id in ("global-one", "market-one")

    loader = MultiSourceSkillLoader(CompositeBackend([Backend()]))

    assert Path(loader.get_skill_base_dir("global-one")).parent == skills_dir
    assert Path(loader.get_skill_base_dir("market-one")).parent == get_user_skills_dir("alice")
    # A private skill's credentials are only ever written under its owner.
    assert not (skills_dir / "market-one").exists()
