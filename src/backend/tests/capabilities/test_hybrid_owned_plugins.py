"""User-owned local packages remain visible in hybrid conversation assembly."""
import pytest
from core.capabilities import plugins, skills
from core.plugins import runtime as plugin_loader
from core.services.desktop_capability_protocol import skill_content_hash


@pytest.mark.parametrize("owner,expected", [("owner", True), (None, False), ("other", False)])
def test_hybrid_keeps_owned_local_plugins_without_legacy_shared_bundles(
    index_db, caps_root, monkeypatch, owner, expected
):
    from core.capabilities import device_catalog

    monkeypatch.setattr(skills, "current_account_profile", lambda: "cloud-profile")
    monkeypatch.setattr(skills, "account_authorized_for", lambda user: True)
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(device_catalog, "active", lambda: True)
    body = "---\nname: native-browser\ndescription: Native browser\n---\nUse browser tools."
    skills.publish_local_skill(
        "native-browser", files={"SKILL.md": body},
        content_hash=skill_content_hash(body, {}), owner_user_id=owner,
    )
    plugins.publish_local_plugin(
        {"slug": "native-browser", "components": {"skills": ["native-browser"]}},
        owner_user_id=owner,
    )
    plan = plugin_loader.resolve_desktop_progressive_plugins(
        user_id="owner", enabled_skill_ids=["native-browser"], enabled_mcp_ids=[]
    )
    assert bool(plan.directory) is expected
