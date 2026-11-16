"""Publication against skill directories this account cannot write.

On a deployed storage volume a user view (``sandbox_skills_u/<uid>``) can be
created by another account, e.g. a container runtime that binds a missing
source creates it as root. That is fixed on the storage host, so publication
only has to say how; a mode without write access stands in for a foreign
owner here (the kernel answers both with EACCES).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root bypasses directory permissions",
)


@pytest.fixture
def skills(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "sandbox_skills"))
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)
    locked = []
    yield tmp_path, locked
    for path in locked:  # let pytest remove the tree
        for p in [path, *path.rglob("*")]:
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o755)


def _lock(locked, path: Path):
    path.chmod(0o555)
    locked.append(path)


def _user_view(user_id="alice") -> Path:
    from core.capabilities.skills.config import get_user_skills_dir

    view = get_user_skills_dir(user_id)
    view.mkdir(parents=True, exist_ok=True)
    return view


def test_permission_error_names_owner_and_fix(skills):
    from core.capabilities.skills.publication import publish_tree

    root, locked = skills
    view = _user_view()
    _lock(locked, view)

    with pytest.raises(PermissionError) as info:
        publish_tree(view / "sample", {"SKILL.md": b"v1"})

    message = str(info.value)
    assert f"backend runs as {os.geteuid()}:{os.getegid()}" in message
    assert f"{view} owner=" in message
    assert "chown -R" in message


def test_undeletable_retired_tree_does_not_fail_republish(skills):
    from core.capabilities.skills.publication import publish_tree

    root, locked = skills
    view = _user_view()
    target = publish_tree(view / "sample", {"SKILL.md": b"v1", "docs/a.md": b"a"})
    # The old tree moves into the staging area, where its files cannot be removed.
    _lock(locked, target / "docs")
    locked.append(root / ".skill-publication")

    publish_tree(view / "sample", {"SKILL.md": b"v2"})

    assert (view / "sample" / "SKILL.md").read_bytes() == b"v2"
    assert not (view / "sample" / "docs").exists()
