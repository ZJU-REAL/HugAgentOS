from features.desktop import plugin_resolution as desktop_plugin_resolution
"""User-owned local packages remain visible in hybrid conversation assembly."""
from features.desktop.capabilities import skill_account
from features.desktop.capabilities import skill_candidates
from features.desktop.capabilities import skill_publication
import pytest
from features.desktop.capabilities import plugins, skills
from core.capabilities.plugins import runtime as plugin_loader
from core.capabilities.manifest_protocol import skill_content_hash


@pytest.mark.parametrize("owner,expected", [("owner", True), (None, False), ("other", False)])
def test_hybrid_keeps_owned_local_plugins_without_legacy_shared_bundles(
    index_db, caps_root, monkeypatch, owner, expected
):
    from features.desktop.capabilities import device_catalog

    monkeypatch.setattr(skill_account, 'current_account_profile', lambda: "cloud-profile")
    monkeypatch.setattr(skill_account, 'account_authorized_for', lambda user: True)
    monkeypatch.setattr(skill_candidates, 'builtin_candidates', lambda: [])
    monkeypatch.setattr(device_catalog, "active", lambda: True)
    body = "---\nname: native-browser\ndescription: Native browser\n---\nUse browser tools."
    skill_publication.publish_local_skill(
        "native-browser", files={"SKILL.md": body},
        content_hash=skill_content_hash(body, {}), owner_user_id=owner,
    )
    plugins.publish_local_plugin(
        {"slug": "native-browser", "components": {"skills": ["native-browser"]}},
        owner_user_id=owner,
    )
    plan = desktop_plugin_resolution.resolve_desktop_progressive_plugins(
        user_id="owner", enabled_skill_ids=["native-browser"], enabled_mcp_ids=[]
    )
    assert bool(plan.directory) is expected
