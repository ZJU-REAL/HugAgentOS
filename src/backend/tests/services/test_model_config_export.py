"""Model config exports exclude credentials and remain importable."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.models.repository import assign_role, create_provider, export_all, get_provider, import_all


@pytest.fixture
def model_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'models.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        yield db
    engine.dispose()


def add_model(db, **overrides):
    return create_provider(
        db,
        **{
            "display_name": "Example",
            "provider_type": "chat",
            "base_url": "https://example.invalid/v1",
            "api_key": "test-private-key",
            "model_name": "example-model",
            "extra_config": {"context_length": 8192},
            **overrides,
        },
    )


def test_export_excludes_credentials_and_keeps_configuration(model_db):
    provider = add_model(model_db)
    assign_role(model_db, "main_agent", provider.provider_id)
    exported = export_all(model_db)
    row = exported["providers"][0]
    assert "api_key" not in row
    assert "test-private-key" not in json.dumps(exported)
    assert row["base_url"] == provider.base_url
    assert row["model_name"] == provider.model_name
    assert row["extra_config"] == {"context_length": 8192}
    assert exported["role_assignments"] == [
        {"role_key": "main_agent", "provider_id": provider.provider_id}
    ]
    assert get_provider(model_db, provider.provider_id).api_key == "test-private-key"


def test_export_excludes_vendor_secrets(model_db):
    add_model(
        model_db,
        provider="bedrock",
        extra_config={
            "aws_region": "us-east-1",
            "aws_access_key_id": "test-access-id",
            "aws_secret_access_key": "test-secret-key",
            "context_length": 8192,
        },
    )
    exported = export_all(model_db)
    assert exported["providers"][0]["extra_config"] == {
        "aws_region": "us-east-1",
        "context_length": 8192,
    }
    assert "test-access-id" not in json.dumps(exported)
    assert "test-secret-key" not in json.dumps(exported)


def test_import_config_preserves_existing_credentials(model_db):
    provider = add_model(
        model_db,
        provider="bedrock",
        extra_config={
            "aws_region": "us-east-1",
            "aws_access_key_id": "test-access-id",
            "aws_secret_access_key": "test-secret-key",
        },
    )
    exported = export_all(model_db)
    exported["providers"][0]["model_name"] = "updated-model"
    exported["providers"][0]["extra_config"]["aws_region"] = "us-west-2"
    import_all(model_db, exported)
    model_db.expire_all()
    saved = get_provider(model_db, provider.provider_id)
    assert saved.model_name == "updated-model"
    assert saved.api_key == "test-private-key"
    assert saved.extra_config == {
        "aws_region": "us-west-2",
        "aws_access_key_id": "test-access-id",
        "aws_secret_access_key": "test-secret-key",
    }


def test_import_new_config_without_key(model_db):
    provider = add_model(model_db)
    assign_role(model_db, "main_agent", provider.provider_id)
    exported = export_all(model_db)
    exported["providers"][0].pop("api_key", None)
    exported["providers"][0]["provider_id"] = "new-provider"
    exported["role_assignments"][0]["provider_id"] = "new-provider"
    result = import_all(model_db, exported)
    assert result == {"imported_providers": 1, "imported_roles": 1}
    assert get_provider(model_db, "new-provider").api_key == ""


def test_legacy_import_accepts_explicit_key(model_db):
    provider = add_model(model_db)
    exported = export_all(model_db)
    exported["providers"][0]["api_key"] = "test-replacement-key"
    import_all(model_db, exported)
    assert get_provider(model_db, provider.provider_id).api_key == "test-replacement-key"
