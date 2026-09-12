import pytest
from fastapi import HTTPException
from core.capabilities.change_merge import revision
from core.db.models import AdminSkill, AdminMcpServer, InstalledPlugin, ContentBlock
from core.services import capability_workcopies as copies


@pytest.fixture
def cloud(index_db, monkeypatch):
    engine = index_db.kw["bind"]
    for model in (AdminSkill, AdminMcpServer, InstalledPlugin):
        model.__table__.create(engine, checkfirst=True)
    monkeypatch.setattr(copies, "SessionLocal", index_db)
    from api.routes.v1 import me_capabilities

    monkeypatch.setattr(me_capabilities, "_require_flag", lambda *a: None)
    return index_db


def test_full_plugin_files_saved_with_receipt_and_cas(cloud):
    files = {"plugin.json": '{"name":"Example"}', "query.json": "{}", "run.log": "log"}
    body = {"files": files, "request_id": "a" * 32, "create_only": True}
    saved = copies.commit("user", "plugin", "example", body)
    assert saved["files"] == files
    assert not saved["applied"]
    assert copies.commit("user", "plugin", "example", body) == saved
    assert copies.snapshot("user", "plugin", "example")["files"] == files
    with pytest.raises(HTTPException) as error:
        copies.commit(
            "user",
            "plugin",
            "example",
            {
                **body,
                "request_id": "b" * 32,
                "create_only": False,
                "expected_revision": revision({}),
            },
        )
    assert error.value.status_code == 409
    next_files = {**files, "query.json": "changed"}
    copies.commit(
        "user",
        "plugin",
        "example",
        {
            **body,
            "files": next_files,
            "request_id": "c" * 32,
            "create_only": False,
            "expected_revision": saved["revision"],
        },
    )
    with cloud() as db:
        versions = db.query(ContentBlock).filter(ContentBlock.id.like("cap-cloud-version:%")).all()
        assert len(versions) == 3
        assert all("files" not in row.payload for row in versions)


def test_global_skill_requires_manifest_authorization(cloud, monkeypatch):
    from core.services import desktop_capability

    with cloud() as db:
        db.add(
            AdminSkill(
                skill_id="hidden",
                display_name="Hidden",
                description="Hidden",
                skill_content="restricted",
                owner_user_id=None,
            )
        )
        db.commit()
    monkeypatch.setattr(
        desktop_capability, "build_user_skill_manifest", lambda *a, **k: {"skills": []}
    )
    with pytest.raises(HTTPException) as error:
        copies.snapshot("user", "skill", "hidden")
    assert error.value.status_code == 404


def test_foreign_private_capability_not_readable_or_writable(cloud):
    with cloud() as db:
        db.add(
            AdminSkill(
                skill_id="private",
                display_name="Private",
                description="Private",
                skill_content="private contents",
                owner_user_id="other",
            )
        )
        db.commit()
    with pytest.raises(HTTPException) as error:
        copies.snapshot("user", "skill", "private")
    assert error.value.status_code == 404
    with pytest.raises(HTTPException):
        copies.commit(
            "user", "skill", "private", {"files": {}, "request_id": "a" * 32, "create_only": True}
        )


def test_secret_ack_required_and_idempotency_payload_fixed(cloud):
    body = {
        "files": {"plugin.json": '{"token": "example"}'},
        "request_id": "a" * 32,
        "create_only": True,
    }
    with pytest.raises(HTTPException) as error:
        copies.commit("user", "plugin", "example", body)
    assert error.value.status_code == 409
    copies.commit("user", "plugin", "example", {**body, "acknowledge_sensitive": True})
    with pytest.raises(HTTPException) as error:
        copies.commit(
            "user",
            "plugin",
            "example",
            {**body, "acknowledge_sensitive": True, "files": {"plugin.json": "{}"}},
        )
    assert error.value.status_code == 409


def test_cloud_routes_keep_known_credentials_on_cloud(cloud, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.v1 import desktop_capability as routes
    from core.services import desktop_capability as service

    secret = "server-secret-value-123"
    monkeypatch.setattr(service, "_known_cloud_secrets", lambda *a, **k: {secret})
    with cloud() as db:
        db.add(
            AdminSkill(
                skill_id="secret",
                display_name="Secret",
                description="Secret",
                skill_content=secret,
                owner_user_id="user",
            )
        )
        db.commit()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user"
    client = TestClient(app)
    assert client.get("/v1/desktop/capability/workcopies/skill/secret").status_code == 422
    assert (
        client.post(
            "/v1/desktop/capability/workcopies/plugin/new",
            json={
                "files": {"plugin.json": '{"name":"' + secret + '"}'},
                "request_id": "d" * 32,
                "create_only": True,
                "acknowledge_sensitive": True,
            },
        ).status_code
        == 422
    )
    assert not copies.snapshot("user", "plugin", "new")["exists"]


def test_skill_submission_preserves_all_files_and_checks_model_changes(cloud, monkeypatch):
    from core.ontology import build_validator
    from core.agent_skills import cache_refresh

    validations = []
    monkeypatch.setattr(
        build_validator, "ensure_ontology_build_valid", lambda *a, **k: validations.append(k)
    )
    monkeypatch.setattr(cache_refresh, "refresh_skill_caches", lambda: None)
    files = {
        "SKILL.md": "---\nname: news\ndescription: News\n---\nInstructions",
        "query.json": "{}",
        "run.log": "complete log",
    }
    saved = copies.commit(
        "user", "skill", "news", {"files": files, "request_id": "e" * 32, "create_only": True}
    )
    assert saved["applied"]
    assert validations[0]["asset_type"] == "skill"
    before = copies.snapshot("user", "skill", "news")
    with cloud() as db:
        row = db.get(AdminSkill, "news")
        assert row.extra_files == {"query.json": "{}", "run.log": "complete log"}
        row.description = "Concurrent metadata edit"
        db.commit()
    with pytest.raises(HTTPException) as error:
        copies.commit(
            "user",
            "skill",
            "news",
            {
                "files": files,
                "request_id": "f" * 32,
                "expected_revision": before["revision"],
                "expected_model_version": before["model_version"],
            },
        )
    assert error.value.status_code == 409


def test_plugin_execution_declarations_are_saved_but_not_activated(cloud, monkeypatch):
    import json
    from core.agent_skills import cache_refresh

    monkeypatch.setattr(cache_refresh, "refresh_skill_caches", lambda: None)
    with cloud() as db:
        row = InstalledPlugin(
            install_id="example@user", slug="example", name="Example", owner_user_id="user"
        )
        db.add(row)
        db.commit()
    before = copies.snapshot("user", "plugin", "example@user")
    definition = json.loads(before["files"]["plugin.json"])
    definition["dependencies"] = {"skills": ["new-component"]}
    saved = copies.commit(
        "user",
        "plugin",
        "example@user",
        {
            "files": {"plugin.json": json.dumps(definition), "query.json": "{}"},
            "request_id": "b" * 32,
            "expected_revision": before["revision"],
            "expected_model_version": before["model_version"],
        },
    )
    assert not saved["applied"]
    with cloud() as db:
        assert (
            copies.active_plugin_files(db, "user", db.get(InstalledPlugin, "example@user")) is None
        )


def test_pending_plugin_update_preserves_previous_active_package(cloud, monkeypatch):
    import json
    from core.agent_skills import cache_refresh
    from core.services.desktop_capability import _plugin_files

    monkeypatch.setattr(cache_refresh, "refresh_skill_caches", lambda: None)
    with cloud() as db:
        db.add(
            InstalledPlugin(
                install_id="example@user", slug="example", name="Example", owner_user_id="user"
            )
        )
        db.commit()
    first = copies.snapshot("user", "plugin", "example@user")
    active_files = {**first["files"], "query.json": "ordinary result", "run.log": "all logs"}
    result = copies.commit(
        "user",
        "plugin",
        "example@user",
        {
            "files": active_files,
            "request_id": "a" * 32,
            "expected_revision": first["revision"],
            "expected_model_version": first["model_version"],
        },
    )
    assert result["applied"]
    second = copies.snapshot("user", "plugin", "example@user")
    definition = json.loads(active_files["plugin.json"])
    definition["hooks"] = {"new": "hook"}
    copies.commit(
        "user",
        "plugin",
        "example@user",
        {
            "files": {**active_files, "plugin.json": json.dumps(definition)},
            "request_id": "b" * 32,
            "expected_revision": second["revision"],
            "expected_model_version": second["model_version"],
        },
    )
    with cloud() as db:
        active = copies.active_plugin_files(db, "user", db.get(InstalledPlugin, "example@user"))
        assert active == active_files
        assert _plugin_files({"_uploaded_files": active}) == active_files
