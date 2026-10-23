from features.desktop.api import desktop_capability
"""Model synchronization must distinguish reasoning labels from credentials."""
from features.desktop.api import capability_dependencies
from features.desktop.api import desktop_capability_state

import pytest
from features.desktop.api import desktop_capability as routes
from core.models.repository import assign_role, create_provider
from features.desktop.capability import service as cap
from features.desktop.capability import credentials as credentials
from features.desktop.capability import models as models
from features.desktop.capability import security as security
from core.models import config as model_config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def model_cloud(monkeypatch, db_session):
    factory = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(security, "SessionLocal", factory)
    monkeypatch.setattr(models, "SessionLocal", factory)
    monkeypatch.setattr(model_config, "SessionLocal", factory)
    monkeypatch.setattr(security, "_user_capability_configs", lambda *a, **k: ([], [], {}))
    cap.invalidate_model_gateway_cache()
    app = FastAPI()
    app.include_router(desktop_capability.router)
    app.dependency_overrides[capability_dependencies.require_capability_user] = lambda: "fixture-user"
    with TestClient(app) as client:
        yield db_session, client


def test_reasoning_levels_survive_model_sync_and_followup_role(model_cloud):
    db, client = model_cloud
    extra = {
        "supports_reasoning_effort": True,
        "reasoning_effort_levels": [
            {"key": "low", "value": "low"},
            {"key": "medium", "value": "medium"},
            {"key": "high", "value": "high"},
            {"key": "xhigh", "value": "xhigh"},
            {"key": "max", "value": 100},
        ],
        "default_reasoning_effort": "low",
    }
    reasoning = create_provider(
        db,
        display_name="Reasoning",
        provider_type="chat",
        provider="openai",
        base_url="https://fixture.example/v1",
        api_key="synthetic-model-credential",
        model_name="fixture-model",
        extra_config=extra,
    )
    auxiliary = create_provider(
        db,
        display_name="Auxiliary",
        provider_type="chat",
        provider="openai",
        base_url="https://fixture.example/v1",
        api_key="synthetic-other-credential",
        model_name="auxiliary-model",
    )
    assert assign_role(db, "followup", auxiliary.provider_id)
    response = client.get("/v1/desktop/capability/models")
    assert response.status_code == 200, response.text
    manifest = response.json()["data"]
    assert len(manifest["providers"]) == 2
    exported = next(p for p in manifest["providers"] if p["provider_id"] == reasoning.provider_id)
    assert exported["extra_config"] == extra
    assert manifest["role_assignments"] == [
        {"role_key": "followup", "provider_id": auxiliary.provider_id}
    ]
    assert "withheld" not in manifest
    assert "synthetic-model-credential" not in response.text


@pytest.mark.parametrize(
    "config",
    [
        {"api_key": "low"},
        {"headers": {"X-Private-Key": "low"}},
        {"extra_config": {"reasoning_effort_levels": [{"key": "low", "api_key": "low"}]}},
    ],
)
def test_real_short_keys_remain_credentials_in_model_config(config):
    assert "low" in credentials._secrets_from_config(config, model_config=True)


def test_reasoning_shape_is_not_an_exemption_for_connector_credentials():
    config = {"extra_config": {"reasoning_effort_levels": [{"key": "low", "value": "low"}]}}
    assert "low" in credentials._secrets_from_config(config)
    assert "low" not in credentials._secrets_from_config(config, model_config=True)


def test_unrecognized_model_level_key_is_still_guarded():
    config = {"extra_config": {"reasoning_effort_levels": [{"key": "synthetic-secret"}]}}
    assert "synthetic-secret" in credentials._secrets_from_config(config, model_config=True)


def test_real_short_credential_in_followup_role_still_returns_422(model_cloud):
    db, client = model_cloud
    create_provider(
        db,
        display_name="Credential owner",
        provider_type="chat",
        provider="openai",
        base_url="https://fixture.example/v1",
        api_key="low",
        model_name="credential-owner",
    )
    followup = create_provider(
        db,
        display_name="Auxiliary",
        provider_type="chat",
        provider="openai",
        base_url="https://fixture.example/v1",
        api_key="synthetic-other-credential",
        model_name="other-model",
    )
    assert assign_role(db, "followup", followup.provider_id)
    response = client.get("/v1/desktop/capability/models")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "integrity_failed"


def test_real_credential_in_reasoning_value_is_withheld(model_cloud):
    db, client = model_cloud
    secret = "synthetic-real-model-key"
    create_provider(
        db,
        display_name="Safe",
        provider_type="chat",
        provider="openai",
        base_url="https://fixture.example/v1",
        api_key=secret,
        model_name="safe-model",
    )
    leaking = create_provider(
        db,
        display_name="Leaking",
        provider_type="chat",
        provider="openai",
        base_url="https://fixture.example/v1",
        api_key="synthetic-other-key",
        model_name="leaking-model",
        extra_config={
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": [{"key": "low", "value": secret}],
            "default_reasoning_effort": "low",
        },
    )
    response = client.get("/v1/desktop/capability/models")
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json()["data"]["withheld"] == [
        {"provider_id": leaking.provider_id, "fields": ["extra_config"]}
    ]


def test_nested_key_inside_reasoning_level_is_still_a_credential():
    config = {
        "extra_config": {"reasoning_effort_levels": [{"key": "low", "settings": {"key": "low"}}]}
    }
    assert "low" in credentials._secrets_from_config(config, model_config=True)
