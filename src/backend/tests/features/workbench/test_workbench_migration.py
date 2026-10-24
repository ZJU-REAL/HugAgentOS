"""WB-20 operator boundary and authenticated projection behavior."""

import json
import stat

import pytest
from features.workbench.migration import apply, inventory, rollback
from features.workbench.migration_snapshot import read_snapshot
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import ContentBlock, UserShadow
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def seeded(db_session):
    db_session.add_all(
        [
            ContentBlock(
                id="app_config",
                payload={
                    "apps": [
                        {
                            "id": "case",
                            "name": "Case",
                            "url": "https://case.example/app",
                            "enabled": True,
                        },
                        {"id": "duplicate", "name": "One", "url": "https://one.example"},
                        {"id": "duplicate", "name": "Two", "url": "https://two.example"},
                        {"id": "bad", "name": "Unsafe", "url": "javascript:alert(1)"},
                    ]
                },
            ),
            # The representative portal explicitly uses independent login.
            ContentBlock(
                id="workbench_external_auth:legacy:case",
                payload={"revision": 1, "mode": "independent"},
            ),
            ContentBlock(
                id="page_config",
                payload={
                    "branding": {"product_name": "Preserved"},
                    "navigation": {
                        "sidebar_items": ["projects", "automation"],
                        "menu_items": ["lab", "mystery"],
                    },
                },
            ),
            UserShadow(
                user_id="alice",
                username="Alice",
                extra_data={
                    "allowed_apps": [],
                    "preferences": {"theme": "dark", "pinned_apps": [], "app_order": ["projects"]},
                },
            ),
            UserShadow(user_id="bob", username="Bob", extra_data={"allowed_apps": None}),
        ]
    )
    db_session.commit()
    return db_session


def http(db):
    from api.routes.v1.content import router as content_router
    from features.workbench.api.workbench import router

    app = FastAPI()
    app.include_router(router)
    app.include_router(content_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="bob", user_center_id="bob", username="Bob"
    )
    return TestClient(app)


def test_readonly_inventory_protected_snapshot_and_idempotent_cutover(seeded, tmp_path):
    before = http(seeded).get("/v1/workbench/catalog").json()["data"]
    report = inventory(seeded, tmp_path / "before.json")
    assert report["quarantined"] == 4  # both duplicates, bad URL and unknown menu ID
    assert "https://" not in json.dumps(report)
    assert stat.S_IMODE((tmp_path / "before.json").stat().st_mode) == 0o600
    assert seeded.get(ContentBlock, "app_config") is not None
    receipt = apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    assert receipt["status"] == "applied"
    assert apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True) == receipt
    after = http(seeded).get("/v1/workbench/catalog").json()["data"]
    case = next(i for i in after["items"] if i["applicationKey"] == "legacy:case")
    assert case["declaration"]["target"]["url"] == "https://case.example/app"
    assert not any(i["declaration"]["id"] in {"duplicate", "bad"} for i in after["items"])
    assert next(i for i in before["items"] if i["applicationKey"] == "legacy:case")["enabled"]
    assert seeded.get(ContentBlock, "app_config") is None
    assert "sidebar_items" not in seeded.get(ContentBlock, "page_config").payload["navigation"]
    assert seeded.get(UserShadow, "alice").extra_data["allowed_apps"] == []
    assert seeded.get(UserShadow, "bob").extra_data["allowed_apps"] is None
    client = http(seeded)
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="alice", user_center_id="alice", username="Alice"
    )
    pref = client.get("/v1/workbench/me/navigation-preferences").json()["data"]
    assert pref["pinned"] == []
    assert pref["order"] == ["builtin:projects"]
    assert not any(
        i["applicationKey"] == "legacy:case"
        for i in client.get("/v1/workbench/catalog").json()["data"]["items"]
    )


def test_snapshot_change_or_live_writes_abort_before_cutover(seeded, tmp_path):
    report = inventory(seeded, tmp_path / "before.json")
    with pytest.raises(ValueError, match="writes_not_frozen"):
        apply(seeded, tmp_path / "before.json", report["digest"])
    seeded.get(UserShadow, "bob").extra_data = {"allowed_apps": ["case"]}
    seeded.commit()
    with pytest.raises(ValueError, match="source_changed"):
        apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    assert seeded.get(ContentBlock, "app_config") is not None


def test_restore_retains_quarantined_raw_source_and_metadata(seeded, tmp_path):
    report = inventory(seeded, tmp_path / "before.json")
    apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    restored = rollback(
        seeded,
        tmp_path / "before.json",
        report["digest"],
        tmp_path / "delta.json",
        writes_frozen=True,
    )
    assert restored["status"] == "rolled_back"
    assert len(seeded.get(ContentBlock, "app_config").payload["apps"]) == 4
    assert seeded.get(UserShadow, "alice").extra_data["preferences"]["pinned_apps"] == []
    assert (
        seeded.get(ContentBlock, "page_config").payload["branding"]["product_name"] == "Preserved"
    )


def test_new_state_write_is_exported_and_blocks_lossy_downgrade(seeded, tmp_path):
    report = inventory(seeded, tmp_path / "before.json")
    apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    seeded.add(
        ContentBlock(id="workbench_application_state:new", payload={"payload": {"draft": "New"}})
    )
    seeded.commit()
    with pytest.raises(ValueError, match="delta_not_representable"):
        rollback(
            seeded,
            tmp_path / "before.json",
            report["digest"],
            tmp_path / "delta.json",
            writes_frozen=True,
        )
    delta = read_snapshot(tmp_path / "delta.json")
    assert delta["delta"]["workbench_application_state:new"]["payload"] == {"draft": "New"}
    assert seeded.get(ContentBlock, "workbench_application_state:new") is not None
    assert seeded.get(ContentBlock, "app_config") is None


def test_content_v1_writes_canonical_rows_and_order_without_dual_source(seeded, tmp_path):
    from api.deps import require_admin, require_config

    client = http(seeded)
    client.app.dependency_overrides[require_config] = lambda: None
    client.app.dependency_overrides[require_admin] = lambda: None
    report = inventory(seeded, tmp_path / "before.json")
    apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    apps = {
        "apps": [
            {"id": "zeta", "name": "Z", "url": "https://z.example/app", "enabled": False},
            {
                "id": "case",
                "name": "Case revised",
                "url": "https://case.example/app",
                "enabled": True,
            },
        ]
    }
    response = client.put("/v1/content/app_config", json={"payload": apps})
    assert response.status_code == 200, response.text
    assert seeded.get(ContentBlock, "app_config") is None
    legacy = client.get("/v1/workbench/admin/legacy-config").json()["data"]["app_config"]
    assert [a["id"] for a in legacy["apps"]] == ["zeta", "case"]
    assert seeded.get(ContentBlock, "workbench_external:case").payload["revision"] == 2
    assert all(
        not i.get("legacy")
        for i in client.get("/v1/workbench/admin/external-applications").json()["data"]["items"]
    )
    snapshot = client.get("/v1/content/docs/export").json()["data"]
    assert snapshot["blocks"]["app_config"]["payload"] == legacy
    assert client.post("/v1/content/docs/import", json=snapshot).status_code == 200
    assert seeded.get(ContentBlock, "app_config") is None
    # Page navigation becomes a policy; branding remains in page_config.
    response = client.put(
        "/v1/content/page_config",
        json={
            "payload": {
                "branding": {"product_name": "New brand"},
                "navigation": {"sidebar_items": ["automation", "projects"], "menu_items": ["lab"]},
            }
        },
    )
    assert response.status_code == 200, response.text
    assert seeded.get(ContentBlock, "workbench_policy").payload["defaultPinned"] == [
        "builtin:automation",
        "builtin:projects",
    ]
    assert "sidebar_items" not in seeded.get(ContentBlock, "page_config").payload["navigation"]


def test_representable_external_update_replays_on_restore(seeded, tmp_path):
    from api.deps import require_config

    client = http(seeded)
    client.app.dependency_overrides[require_config] = lambda: None
    report = inventory(seeded, tmp_path / "before.json")
    apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    response = client.put(
        "/v1/content/app_config",
        json={
            "payload": {
                "apps": [
                    {
                        "id": "case",
                        "name": "New name",
                        "url": "https://case.example/new",
                        "enabled": False,
                    }
                ]
            }
        },
    )
    assert response.status_code == 200, response.text
    rollback(
        seeded,
        tmp_path / "before.json",
        report["digest"],
        tmp_path / "delta.json",
        writes_frozen=True,
    )
    restored = seeded.get(ContentBlock, "app_config").payload["apps"]
    assert restored[0]["name"] == "New name"
    assert restored[0]["url"] == "https://case.example/new"
    assert restored[0]["enabled"] is False
    assert len(restored) == 4  # original quarantined data is preserved, not overwritten


def test_failure_after_staging_restores_source_transaction(seeded, tmp_path):
    report = inventory(seeded, tmp_path / "before.json")
    tmp_path.chmod(0o755)  # after-snapshot write must fail after DB changes are staged
    try:
        with pytest.raises(ValueError, match="snapshot_directory_not_private"):
            apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    finally:
        tmp_path.chmod(0o700)
    assert seeded.get(ContentBlock, "app_config") is not None
    assert seeded.get(ContentBlock, "workbench_external:case") is None
    assert seeded.get(UserShadow, "alice").extra_data["preferences"]["pinned_apps"] == []


def test_auth_binding_and_same_service_origin_survive_restore(seeded, tmp_path):
    seeded.add(ContentBlock(id="workbench_origin_identity", payload={"id": "a" * 32}))
    seeded.get(ContentBlock, "workbench_external_auth:legacy:case").payload = {
        "revision": 7,
        "mode": "exchange",
        "secretHash": "b" * 64,
        "audience": "case",
        "callback": "https://case.example/callback",
        "initiationUrl": "https://case.example/login/workbench",
        "ttlSeconds": 30,
    }
    seeded.commit()
    report = inventory(seeded, tmp_path / "before.json")
    assert report["authGaps"] == []
    apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    assert seeded.get(ContentBlock, "workbench_external_auth:legacy:case").payload["revision"] == 7
    rollback(
        seeded,
        tmp_path / "before.json",
        report["digest"],
        tmp_path / "delta.json",
        writes_frozen=True,
    )
    assert seeded.get(ContentBlock, "workbench_origin_identity").payload["id"] == "a" * 32


def test_clone_rotates_origin_without_remapping_plugin_permission(seeded, tmp_path):
    from features.workbench.migration import clone_origin

    seeded.add(ContentBlock(id="workbench_origin_identity", payload={"id": "a" * 32}))
    seeded.get(UserShadow, "bob").extra_data = {"allowed_apps": ["plugin:old:exact:key"]}
    seeded.commit()
    report = inventory(seeded, tmp_path / "before.json")
    clone_origin(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    assert seeded.get(ContentBlock, "workbench_origin_identity").payload["id"] != "a" * 32
    assert seeded.get(UserShadow, "bob").extra_data["allowed_apps"] == ["plugin:old:exact:key"]


def test_personal_null_empty_force_all_and_role_team_inheritance_do_not_expand(seeded, tmp_path):
    from core.auth.capabilities import resolve_user_capabilities
    from core.db.models import Role, RoleAssignment, Team, TeamMember

    seeded.add_all(
        [
            Role(role_id="case-role", name="Case role", permissions={"allowed_apps": ["case"]}),
            Team(
                team_id="case-team",
                name="Case team",
                default_permissions={"allowed_apps": ["automation"]},
            ),
            TeamMember(team_id="case-team", user_id="bob", role="member"),
            RoleAssignment(role_id="case-role", principal_type="team", principal_id="case-team"),
            UserShadow(user_id="all", username="All", extra_data={"allowed_apps": "*"}),
        ]
    )
    seeded.commit()
    before = {
        uid: resolve_user_capabilities(seeded, uid)["allowed_apps"]
        for uid in ["alice", "bob", "all"]
    }
    assert before == {"alice": [], "bob": ["case"], "all": None}
    report = inventory(seeded, tmp_path / "before.json")
    apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    after = {
        uid: resolve_user_capabilities(seeded, uid)["allowed_apps"]
        for uid in ["alice", "bob", "all"]
    }
    assert after == before


def test_reapply_after_restore_requires_fresh_inventory(seeded, tmp_path):
    report = inventory(seeded, tmp_path / "before.json")
    apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    rollback(
        seeded,
        tmp_path / "before.json",
        report["digest"],
        tmp_path / "delta.json",
        writes_frozen=True,
    )
    with pytest.raises(ValueError, match="migration_new_inventory_required"):
        apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    next_report = inventory(seeded, tmp_path / "next.json")
    next_receipt = apply(seeded, tmp_path / "next.json", next_report["digest"], writes_frozen=True)
    assert next_receipt["status"] == "applied"
    assert next_receipt["revision"] == 3


def test_unknown_fields_and_plugin_contracts_never_create_grants(seeded, tmp_path):
    from core.db.models import InstalledPlugin

    seeded.add(
        InstalledPlugin(
            install_id="old@bob",
            slug="old",
            name="Old",
            owner_user_id="bob",
            ui_contributions={"version": 2, "contributes": {"modules": []}},
            component_ids={},
            import_report={},
        )
    )
    seeded.get(UserShadow, "bob").extra_data = {"allowed_apps": ["case", "future", "future"]}
    seeded.commit()
    report = inventory(seeded, tmp_path / "before.json")
    assert report["pluginVersions"] == {"2": 1}
    assert len(report["permissionReferences"]) == 2
    apply(seeded, tmp_path / "before.json", report["digest"], writes_frozen=True)
    assert seeded.get(InstalledPlugin, "old@bob").ui_contributions["version"] == 2
    assert seeded.get(UserShadow, "bob").extra_data["allowed_apps"] == ["case", "future", "future"]
