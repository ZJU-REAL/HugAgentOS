"""The real skill loader with the desktop store enabled: resolver-driven merge, no priority override."""

from __future__ import annotations

import base64
import json
import time

import pytest
from core.agent_skills import config as skill_config
from core.agent_skills.loader import MultiSourceSkillLoader
from core.capabilities import registry, skills, store
from core.capabilities.paths import KIND_SKILL
from core.capabilities.ref import cloud_ref, profile_id
from core.services import desktop_cloud_bridge as bridge


def _md(sid: str, body: str) -> str:
    return f"---\nname: {sid}\ndescription: {sid} desc\n---\n{body}\n"


def _token(uid: str) -> str:
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"u": uid, "c": "center-" + uid, "a": 1, "h": "session-" + uid, "d": "device"}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"dcap2.{body}.sig"


STATE = {"cloud_base": "https://cloud.example", "token": _token("u-1"), "expires_at": 0}
PROFILE = profile_id(STATE["cloud_base"], "u-1")


@pytest.fixture
def env(tmp_path, monkeypatch, index_db, caps_root):
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "ws" / "skills"))
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "s")
    monkeypatch.setenv("HUGAGENT_DISABLE_PROJECT_SKILLS", "1")
    builtin = tmp_path / "builtin"
    (builtin / "ppt-design").mkdir(parents=True)
    (builtin / "ppt-design" / "SKILL.md").write_text(_md("ppt-design", "shipped"))
    monkeypatch.setattr(skill_config, "_builtin_skills_dir", lambda: builtin)
    monkeypatch.setattr(skills, "builtin_dir", lambda: builtin)
    # The loader's built-in source is derived from the package path; point it at the fixture.
    real_sources = skill_config.get_default_skill_sources

    def _sources():
        out = []
        for s in real_sources():
            if s.name == "built-in":
                s = skill_config.SkillSourceConfig("built-in", builtin, s.priority, True)
            if s.name == "admin":
                continue  # no database catalog in this test
            out.append(s)
        return out

    monkeypatch.setattr(skill_config, "get_default_skill_sources", _sources)
    monkeypatch.setattr(
        "core.agent_skills.loader.get_enabled_skill_sources",
        lambda: [s for s in _sources() if s.enabled],
    )
    bridge.reset_for_tests()
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(bridge, "get_state", lambda: dict(STATE, expires_at=time.time() + 60))
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "u-1")
    yield builtin
    bridge.reset_for_tests()


def _install_cloud(sid: str, body: str) -> str:
    md = _md(sid, body)
    from core.services.desktop_capability_protocol import skill_content_hash

    h = skill_content_hash(md, {})
    inst = registry.upsert(
        profile_id=PROFILE,
        ref=cloud_ref(STATE["cloud_base"], KIND_SKILL, sid, scope="shared"),
        content_hash=h,
    )
    from core.capabilities.paths import revision_for_hash

    store.write_from_files(KIND_SKILL, PROFILE, sid, revision_for_hash(h), {"SKILL.md": md})
    registry.set_state(inst.install_id, "ready", resolved_revision=revision_for_hash(h))
    return inst.install_id


def test_loader_builds_with_store_and_resolves_same_id(env):
    loader = MultiSourceSkillLoader()  # must not raise (the merge hook runs inside __init__)
    meta = loader.load_all_metadata()
    assert set(meta) == {"ppt-design"} and meta["ppt-design"].skill_path.startswith("built-in:")

    _install_cloud("ppt-design", "from cloud")
    _install_cloud("market-x", "cloud only")
    loader = MultiSourceSkillLoader()
    meta = loader.load_all_metadata()
    assert set(meta) == {"ppt-design", "market-x"}
    assert meta["ppt-design"].skill_path.startswith(
        "cloud:"
    )  # account-level wins, built-in shadowed
    res = skills.loader_resolution()
    assert res.reasons["ppt-design"] == "account_unique" and not res.conflicts
    spec = loader.load_skill_full("ppt-design")
    assert "from cloud" in spec.instructions and loader.get_skill_source("market-x") == "cloud"

    registry.set_preference(KIND_SKILL, "ppt-design", "skill:builtin:ppt-design", chosen_by="u-1")
    loader = MultiSourceSkillLoader()
    assert loader.load_all_metadata()["ppt-design"].skill_path.startswith("built-in:")
    assert skills.loader_resolution().reasons["ppt-design"] == "preference"
