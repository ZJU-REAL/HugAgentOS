import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db.engine import Base
from core.db.audio_model_upgrade import reconcile_audio_model_type
from core.models.repository import create_provider, assign_role, get_provider, import_all, export_all
from core.models.providers.registry import validate_payload
from core.models.config import ModelConfigService
from core.models import config as model_config


@pytest.fixture
def model_db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        yield db
    engine.dispose()


def add(db, kind="audio", name="asr", url=""):
    return create_provider(db, display_name=name, provider_type=kind, provider="audio_compatible" if kind == "audio" else "openai_compatible", base_url=url, api_key="", model_name=name)


def test_audio_role_is_independent_and_disabled_when_unassigned(model_db, monkeypatch):
    monkeypatch.setattr(model_config, "SessionLocal", sessionmaker(bind=model_db.get_bind()))
    service=ModelConfigService()
    assert service.resolve("audio_transcription") is None
    row=add(model_db)
    assign_role(model_db,"audio_transcription",row.provider_id)
    assert service.resolve("audio_transcription").model_name=="asr"
    assert service.resolve("main_agent") is None
    assert service.resolve_provider(row.provider_id) is None
    assert service.resolve_failover_chain(None)==[]
    assert validate_payload("audio_compatible","audio",{}) is None
    assert validate_payload("audio_compatible","chat",{})


def test_import_rejects_invalid_graph_before_mutating(model_db):
    row=add(model_db,"chat","main")
    assign_role(model_db,"main_agent",row.provider_id)
    exported=export_all(model_db)
    exported['providers'][0].update(provider_type="audio",provider="audio_compatible",display_name="changed")
    with pytest.raises(ValueError): import_all(model_db,exported)
    assert get_provider(model_db,row.provider_id).display_name=="main"
    audio=add(model_db)
    with pytest.raises(ValueError): import_all(model_db,{"role_assignments":[{"role_key":"main_agent","provider_id":audio.provider_id}]})
    # Retained assignments must be checked even when omitted from the imported payload.
    with pytest.raises(ValueError): import_all(model_db,{"providers":[{"provider_id":row.provider_id,"provider_type":"audio","provider":"audio_compatible"}]})
    valid=export_all(model_db)
    assert import_all(model_db,valid)['imported_providers']==2


def test_sqlite_upgrade_keeps_models_and_roles_and_refuses_loss(model_db):
    engine=model_db.get_bind()
    row=add(model_db,"chat","main")
    assign_role(model_db,"main_agent",row.provider_id)
    path=Path(__file__).parents[2]/"alembic/versions/voiceaudio01_audio_model_type.py"
    spec=importlib.util.spec_from_file_location("audio_migration",path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    model_db.commit()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)): migration.downgrade()
    reconcile_audio_model_type(engine)
    reconcile_audio_model_type(engine)
    model_db.expire_all()
    assert get_provider(model_db,row.provider_id).model_name=="main"
    assert model_db.execute(__import__('sqlalchemy').text("SELECT role_key FROM model_role_assignments")).scalar()=="main_agent"
    audio=add(model_db)
    with pytest.raises(RuntimeError):
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)): migration.downgrade()
    model_db.expire_all()
    assert get_provider(model_db,audio.provider_id)
    assert "'audio'" in next(item for item in inspect(engine).get_check_constraints("model_providers") if item["name"]=="model_providers_type_check")["sqltext"]


def test_audio_management_can_save_blank_endpoint_and_enforces_role_type(model_db, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from api.routes.v1 import models
    monkeypatch.setattr(models, "_invalidate_model_caches", lambda: None)
    monkeypatch.setattr("core.db.engine.SessionLocal", sessionmaker(bind=model_db.get_bind()))
    created = asyncio.run(models.create_provider_endpoint(
        models.ProviderCreateRequest(display_name="待配置音频", provider_type="audio", provider="audio_compatible", base_url="", api_key="", model_name="pending-asr"), None))
    data = created["data"]
    row = get_provider(model_db, data["provider_id"])
    assert row.base_url == "" and row.api_key == ""
    with pytest.raises(HTTPException) as error:
        models.assign_role_endpoint("main_agent", models.RoleAssignRequest(provider_id=row.provider_id), None, model_db)
    assert error.value.status_code == 400
    result = models.assign_role_endpoint("audio_transcription", models.RoleAssignRequest(provider_id=row.provider_id), None, model_db)
    assert result["data"]["provider_id"] == row.provider_id
    with pytest.raises(HTTPException):
        asyncio.run(models.update_provider_endpoint(row.provider_id, models.ProviderUpdateRequest(provider_type="chat", provider="openai_compatible"), None))
