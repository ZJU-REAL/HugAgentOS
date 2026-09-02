"""Cube pushes skill files on demand — it must not push someone else's private skill.

Cube has no bind mounts: the skill ids referenced by a command are resolved on the
backend and their directories are uploaded into that session's sandbox. The ids come
out of a model-written command, so without an ownership check any user could name
another user's market-installed skill and have its files — secrets.json included —
copied into their own sandbox.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _Loader:
    def __init__(self, owners: dict[str, str | None], dirs: dict[str, str]):
        self._owners = owners
        self._dirs = dirs

    def get_skill_owner(self, skill_id):
        return self._owners.get(skill_id)

    def get_skill_dir(self, skill_id):
        return self._dirs.get(skill_id)


@pytest.fixture()
def provider(monkeypatch, tmp_path):
    from core.sandbox.cube_provider import CubeSandboxProvider

    p = CubeSandboxProvider.__new__(CubeSandboxProvider)
    p._materialized_skills = {}
    p._builtin_skill_ids = set()
    p._push_skill_dir = AsyncMock()

    dirs = {}
    for sid in ("shared-skill", "alice-market-6533a8", "bob-market-991dc1"):
        d = tmp_path / sid
        d.mkdir()
        (d / "SKILL.md").write_text("# skill", encoding="utf-8")
        dirs[sid] = str(d)
    owners = {
        "shared-skill": None,
        "alice-market-6533a8": "alice",
        "bob-market-991dc1": "bob",
    }

    import core.agent_skills.loader as loader_mod

    monkeypatch.setattr(loader_mod, "get_skill_loader", lambda *a, **k: _Loader(owners, dirs))
    return p


def _pushed(provider) -> set[str]:
    return {call.args[1] for call in provider._push_skill_dir.await_args_list}


def test_pushes_shared_and_own_private_skills(provider) -> None:
    sbx = SimpleNamespace(sandbox_id="sbx-1")
    cmd = "cd /workspace/skills/shared-skill && cd /workspace/skills/alice-market-6533a8"

    asyncio.run(provider._materialize_referenced_skills(sbx, cmd, "alice"))

    assert _pushed(provider) == {"shared-skill", "alice-market-6533a8"}


def test_refuses_another_users_private_skill(provider) -> None:
    sbx = SimpleNamespace(sandbox_id="sbx-1")
    cmd = "cat /workspace/skills/bob-market-991dc1/secrets.json"

    asyncio.run(provider._materialize_referenced_skills(sbx, cmd, "alice"))

    assert _pushed(provider) == set()


def test_refuses_private_skills_for_a_sessionless_run(provider) -> None:
    # An ephemeral run with no bound user gets shared skills only.
    sbx = SimpleNamespace(sandbox_id="sbx-1")
    cmd = "cd /workspace/skills/shared-skill && cd /workspace/skills/bob-market-991dc1"

    asyncio.run(provider._materialize_referenced_skills(sbx, cmd, None))

    assert _pushed(provider) == {"shared-skill"}
