from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from api.deps import require_admin, require_ontology_governance
from core.auth.backend import UserContext, get_current_user
from core.db.engine import Base, get_db
from core.db.models import UserShadow
from core.services.ontology_service import OntologyService
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def ontology_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        db.add(UserShadow(user_id="onto_api_user", username="ontology user", extra_data={}))
        payload_path = (
            Path(__file__).resolve().parents[2]
            / "configs"
            / "ontology_packs"
            / "enterprise_risk_v1.json"
        )
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        service = OntologyService(db)
        service.create_version(payload, actor_id="test", activate=True)
        service.set_pack_flags("enterprise_risk", is_enabled=True, is_default=True)

    from api.app import app

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="onto_api_user",
        user_center_id="onto_api_user",
        username="ontology user",
    )
    app.dependency_overrides[require_admin] = lambda: True
    app.dependency_overrides[require_ontology_governance] = lambda: "onto_api_user"
    client = TestClient(app)
    client.ontology_session_factory = session_factory
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)


def test_user_can_opt_in_and_preview_runtime(ontology_client):
    initial = ontology_client.get("/v1/ontologies/settings")
    assert initial.status_code == 200
    assert initial.json()["data"]["ontology_enabled"] is False
    assert initial.json()["data"]["available"] is True

    updated = ontology_client.patch(
        "/v1/ontologies/settings",
        json={"ontology_enabled": True},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["ontology_enabled"] is True

    preview = ontology_client.get(
        "/v1/ontologies/runtime/preview",
        params={"task": "请生成企业风险与风险预警报告"},
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["enabled"] is True
    assert preview.json()["data"]["review_level"] == "committee"

    disabled = ontology_client.patch(
        "/v1/ontologies/settings",
        json={"ontology_enabled": False},
    )
    assert disabled.status_code == 200
    with ontology_client.ontology_session_factory() as db:
        OntologyService(db).set_pack_flags("enterprise_risk", is_enabled=False)
    unavailable = ontology_client.patch(
        "/v1/ontologies/settings",
        json={"ontology_enabled": True},
    )
    assert unavailable.status_code == 400


def test_admin_metrics_endpoint_has_closed_loop_counters(ontology_client):
    response = ontology_client.get("/v1/admin/ontologies/metrics")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["events_total"] == 0
    assert data["reviews_total"] == 0
    assert data["drafts_total"] == 0
    assert data["source_acceptance"] == {}
    assert data["daily_30d"] == []


def test_ce_governance_alias_exposes_the_same_management_surface(ontology_client):
    packs = ontology_client.get("/v1/ontologies/governance")
    metrics = ontology_client.get("/v1/ontologies/governance/metrics")

    assert packs.status_code == 200
    assert packs.json()["data"]["items"][0]["pack_id"] == "enterprise_risk"
    assert metrics.status_code == 200
    assert metrics.json()["data"]["events_total"] == 0


def test_admin_module_edits_reuse_one_working_draft(ontology_client):
    packs = ontology_client.get("/v1/admin/ontologies").json()["data"]["items"]
    pack = next(item for item in packs if item["pack_id"] == "enterprise_risk")
    active = next(
        item for item in pack["versions"] if item["version_id"] == pack["active_version_id"]
    )
    document = ontology_client.get(
        f"/v1/admin/ontologies/enterprise_risk/versions/{active['version_id']}/export"
    ).json()["data"]

    first_document = copy.deepcopy(document)
    first_document["version"] = "1.1.1"
    first_document["name"] = "工作草稿名称"
    first_document["description"] = "工作草稿第一次保存"
    created = ontology_client.put(
        "/v1/admin/ontologies/enterprise_risk/draft",
        json={"document": first_document},
    )
    assert created.status_code == 200
    created_data = created.json()["data"]
    assert created_data["created"] is True
    assert created_data["status"] == "draft"

    competing_document = copy.deepcopy(first_document)
    competing_document["version"] = "1.1.2"
    competing_import = ontology_client.post(
        "/v1/admin/ontologies/versions",
        json={"document": competing_document, "activate": False},
    )
    assert competing_import.status_code == 400
    assert "已有工作草稿" in competing_import.json()["message"]

    second_document = copy.deepcopy(created_data["content"])
    second_document["description"] = "工作草稿第二次保存"
    updated = ontology_client.put(
        "/v1/admin/ontologies/enterprise_risk/draft",
        json={
            "document": second_document,
            "draft_version_id": created_data["version_id"],
            "expected_checksum": created_data["checksum"],
        },
    )
    assert updated.status_code == 200
    updated_data = updated.json()["data"]
    assert updated_data["created"] is False
    assert updated_data["version_id"] == created_data["version_id"]
    assert updated_data["content"]["description"] == "工作草稿第二次保存"

    stale_update = ontology_client.put(
        "/v1/admin/ontologies/enterprise_risk/draft",
        json={
            "document": second_document,
            "draft_version_id": created_data["version_id"],
            "expected_checksum": created_data["checksum"],
        },
    )
    assert stale_update.status_code == 400
    assert "其他管理员更新" in stale_update.json()["message"]

    packs = ontology_client.get("/v1/admin/ontologies").json()["data"]["items"]
    pack = next(item for item in packs if item["pack_id"] == "enterprise_risk")
    assert pack["working_draft_version_id"] == created_data["version_id"]
    assert len(pack["versions"]) == 2
    assert pack["name"] != "工作草稿名称"

    published = ontology_client.post(
        f"/v1/admin/ontologies/enterprise_risk/versions/{created_data['version_id']}/activate"
    )
    assert published.status_code == 200
    assert published.json()["data"]["status"] == "active"
    packs = ontology_client.get("/v1/admin/ontologies").json()["data"]["items"]
    pack = next(item for item in packs if item["pack_id"] == "enterprise_risk")
    assert pack["name"] == "工作草稿名称"

    next_document = copy.deepcopy(updated_data["content"])
    next_document["version"] = "1.1.2"
    next_document["name"] = "应被放弃的名称"
    next_draft = ontology_client.put(
        "/v1/admin/ontologies/enterprise_risk/draft",
        json={"document": next_document},
    ).json()["data"]
    discarded = ontology_client.delete(
        f"/v1/admin/ontologies/enterprise_risk/draft/{next_draft['version_id']}"
    )
    assert discarded.status_code == 200
    packs = ontology_client.get("/v1/admin/ontologies").json()["data"]["items"]
    pack = next(item for item in packs if item["pack_id"] == "enterprise_risk")
    assert pack["working_draft_version_id"] is None
    assert len(pack["versions"]) == 2
    assert pack["name"] == "工作草稿名称"
