"""The actual /skill, /connector and /plugin API resolves desktop-only sources."""

import pytest
from fastapi import HTTPException

from api.routes.v1.chats import _resolve_explicit_capability_invocation
from api.schemas import ChatRequest
from core.capabilities import registry, skills, store
from core.capabilities.paths import revision_for_hash
from core.config import catalog_resolver
from core.services import desktop_cloud_bridge as bridge
from tests.capabilities.test_cloud_plugin_binding_runtime import cloud_plugin


@pytest.fixture
def explicit_cloud(index_db, caps_root, monkeypatch):
    state, plugin, component, skill = cloud_plugin(monkeypatch, index_db)
    from core.db.models import AdminSkill, AdminMcpServer, InstalledPlugin

    with index_db() as db:
        for model in (AdminSkill, AdminMcpServer, InstalledPlugin):
            model.__table__.create(db.get_bind(), checkfirst=True)
    prepared = store.write_from_files(
        "skill",
        skill.profile_id,
        skill.key,
        revision_for_hash(skill.content_hash),
        {"SKILL.md": "private script"},
    )
    registry.set_state(skill.install_id, "ready", resolved_revision=prepared.revision)
    monkeypatch.setattr(
        catalog_resolver, "get_runtime_catalog", lambda *a, **k: {"skills": [], "mcp": []}
    )
    monkeypatch.setattr(bridge, "_local_server_base_map", lambda: {})
    monkeypatch.setattr(bridge, "_mcp_json_local_declarations", lambda: {})
    monkeypatch.setattr(bridge, "keep_local_bases", lambda: set())
    context = {
        "profile": skill.profile_id,
        "servers": [{"server_id": "pack-search", "tools": [{"name": "search"}]}],
    }
    monkeypatch.setattr(bridge, "_bridge_context", lambda: context)
    return index_db, plugin, skill, context


@pytest.mark.parametrize(
    "field, selected",
    [
        ("skill_id", "pack-skill"),
        ("connector_id", "pack-search"),
        ("plugin_id", "pack@cloud-owner"),
    ],
)
def test_real_chat_selection_accepts_authorized_cloud_only_source(explicit_cloud, field, selected):
    factory, plugin, skill, context = explicit_cloud
    with factory() as db:
        request = _resolve_explicit_capability_invocation(
            db,
            ChatRequest(
                chat_id="test-explicit", message="use selected capability", **{field: selected}
            ),
            "local-owner",
        )
    if field == "plugin_id":
        assert request.plugin_id == plugin.install_id
        assert request._resolved_plugin_skill_ids == ["pack-skill"]
        assert request._resolved_plugin_mcp_ids == ["pack-search"]
    elif field == "connector_id":
        assert request._resolved_mcp_ids == ["pack-search"]
    else:
        assert request.skill_id == "pack-skill"


@pytest.mark.parametrize(
    "field, selected",
    [
        ("skill_id", "pack-skill"),
        ("connector_id", "pack-search"),
        ("plugin_id", "pack@cloud-owner"),
    ],
)
def test_real_chat_selection_rejects_other_account(explicit_cloud, field, selected):
    factory, plugin, skill, context = explicit_cloud
    with factory() as db, pytest.raises(HTTPException) as denied:
        _resolve_explicit_capability_invocation(
            db,
            ChatRequest(chat_id="test-explicit", message="use", **{field: selected}),
            "another-user",
        )
    assert denied.value.status_code == 403


def test_empty_mcp_schema_and_disabled_cloud_skill_cannot_be_revived(explicit_cloud):
    factory, plugin, skill, context = explicit_cloud
    registry.set_enabled(skill.install_id, False)
    context["servers"][0]["tools"] = []
    with factory() as db:
        allowed = catalog_resolver.resolve_explicit_runtime_capabilities(
            db, "local-owner", skill_ids=["pack-skill"], mcp_ids=["pack-search"]
        )
    assert allowed == ([], [], ["pack-skill"], ["pack-search"])


def test_cloud_skill_name_conflict_remains_blocked_for_explicit_selection(explicit_cloud):
    factory, plugin, skill, context = explicit_cloud
    from core.services.desktop_capability_protocol import skill_content_hash

    body = "different local bytes"
    skills.publish_local_skill(
        "pack-skill",
        files={"SKILL.md": body},
        content_hash=skill_content_hash(body, {}),
        owner_user_id="local-owner",
    )
    with factory() as db, pytest.raises(HTTPException) as denied:
        _resolve_explicit_capability_invocation(
            db,
            ChatRequest(chat_id="test-explicit", message="use", skill_id="pack-skill"),
            "local-owner",
        )
    assert denied.value.status_code == 403
