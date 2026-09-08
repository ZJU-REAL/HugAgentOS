"""Synthetic declaration export checks; no user configuration or credentials."""

import json
import pytest
from core.services import desktop_capability as cap


def agent(data):
    return json.loads(
        cap._agent_files({"agent_id": "writer", "name": "Writer", **data})["agent.json"]
    )


def plugin(data):
    return json.loads(
        cap._plugin_files({"install_id": "pack@user", "slug": "pack", "name": "Pack", **data})[
            "plugin.json"
        ]
    )


DECLARATION = {
    "kind": "skill",
    "key": "writer",
    "required": False,
    "version_constraint": ">=2",
    "platforms": ["windows"],
}


def test_agent_top_level_declarations_survive_export():
    fields = {
        "dependencies": [DECLARATION],
        "platforms": ["windows"],
        "extensions": {"python_version": ">=3.11"},
    }
    result = agent(fields)
    for key, value in fields.items():
        assert result[key] == value


@pytest.mark.parametrize(
    "requirements",
    [
        [DECLARATION],
        {
            "dependencies": [DECLARATION],
            "platforms": ["windows"],
            "components": {"plugins": [{"id": "pack", "required": True}]},
        },
    ],
)
def test_agent_extra_retains_only_explicit_public_declarations(requirements):
    extra = {
        "version": "V2",
        "ontology_tags": ["writing"],
        "capability_requirements": requirements,
        "change_history": [{"body": "private-history"}],
        "custom_backend": {"url": "https://private.example", "header": "private-key"},
    }
    result = agent({"extra_config": extra})
    assert result["extra_config"] == {
        key: extra[key] for key in ("version", "ontology_tags", "capability_requirements")
    }


def test_plugin_all_components_and_dependency_constraints_survive():
    fields = {
        "components": {
            "skills": [DECLARATION],
            "agents": ["writer"],
            "mcp": [{"id": "search", "required": True}],
            "plugins": [{"key": "child", "version": ">=1"}],
            "hooks": [{"id": "unknown", "required": True}],
        },
        "dependencies": [{"kind": "pip", "id": "python-docx", "version_constraint": ">=1"}],
        "platforms": ["windows"],
        "extensions": [{"id": "future-runtime", "required": True}],
    }
    result = plugin(fields)
    for key, value in fields.items():
        assert result[key] == value


def test_declaration_arbitrary_connection_payload_is_not_exported():
    dirty = {
        **DECLARATION,
        "headers": {"Authorization": "private-key"},
        "env": {"API_KEY": "private-key"},
        "url": "https://private.example",
    }
    result = plugin(
        {
            "components": {"skills": [dirty]},
            "extensions": {"python_version": ">=3.11", "headers": {"Authorization": "private-key"}},
        }
    )
    assert result["components"]["skills"] == [DECLARATION]
    assert result["extensions"]["python_version"] == ">=3.11"
    assert "private-key" not in json.dumps(result) and "private.example" not in json.dumps(result)


def test_exported_declaration_canary_still_hits_content_guard(monkeypatch):
    monkeypatch.setattr(cap, "_known_cloud_secrets", lambda uid: {"synthetic-declared-secret"})
    result = agent(
        {"dependencies": [{"kind": "skill", "id": "synthetic-declared-secret", "required": True}]}
    )
    with pytest.raises(cap.CapabilityContentRejected):
        cap.guard_capability_content("user", result)


def test_legacy_plugin_lists_remain_compatible():
    result = plugin({"skills": ["legacy"], "mcp": ["search"]})
    assert result["components"]["skills"] == ["legacy"]
    assert result["components"]["mcp"] == ["search"]


def test_optional_unknown_extension_remains_optional_without_executable_payload():
    from core.capabilities.dependency import _entries

    result = agent(
        {"extensions": {"future_hook": {"required": False, "script": "secret execution content"}}}
    )
    assert result["extensions"] == {"future_hook": {"required": False}}
    entries = _entries(result, "agent")
    assert entries == [{"id": "future_hook", "required": False, "kind": "unsupported_extension"}]


def test_declaration_edits_change_manifest_content_hash(monkeypatch):
    definition = {"agent_id": "writer", "name": "Writer", "dependencies": [dict(DECLARATION)]}
    monkeypatch.setattr(cap, "_user_agents", lambda uid: [definition])
    before = cap.build_user_agent_manifest("user", use_cache=False)
    definition["dependencies"][0]["version_constraint"] = ">=3"
    after = cap.build_user_agent_manifest("user", use_cache=False)
    assert before["entries"][0]["content_hash"] != after["entries"][0]["content_hash"]
    assert before["revision"] != after["revision"]


from tests.capabilities.test_agents_plugins import cloud_db


def test_database_component_metadata_reaches_plugin_bundle(cloud_db):
    import io
    import zipfile
    from core.db.models import InstalledPlugin

    components = {
        "skills": [DECLARATION],
        "agents": ["writer"],
        "mcp": [{"id": "search", "required": False}],
        "plugins": [{"id": "child", "version_constraint": ">=2"}],
    }
    with cloud_db() as db:
        row = db.query(InstalledPlugin).filter_by(install_id="sites@cloud-u").one()
        row.component_ids = components
        db.commit()
    rows = cap._user_plugins("cloud-u")
    assert rows[0]["components"] == components
    data, _ = cap.resolve_plugin_bundle("cloud-u", "sites@cloud-u")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        definition = json.loads(archive.read("sites/plugin.json"))
    assert definition["components"] == components


def test_invalid_declaration_returns_fixed_http_error(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.v1 import desktop_capability as routes

    monkeypatch.setattr(cap, "_known_cloud_secrets", lambda uid: set())
    monkeypatch.setattr(
        cap,
        "_user_agents",
        lambda uid: [
            {
                "agent_id": "writer",
                "name": "Writer",
                "dependencies": [{"id": {"private": "synthetic-secret"}}],
            }
        ],
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user"
    with TestClient(app) as client:
        response = client.get("/v1/desktop/capability/agents/writer/bundle")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "integrity_failed"
    assert "synthetic-secret" not in response.text
