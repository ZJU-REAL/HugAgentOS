import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.v1 import audio
from core.db.engine import Base, get_db
from core.db.models import UserShadow
from core.models.repository import create_provider, assign_role, update_provider
from core.chat.audio_transcription import resolve_audio_model
from tests.services.test_audio_transcription import wav
from tests.db_support import share_session


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        db.add(UserShadow(user_id="owner", username="owner", extra_data={"can_use_voice": True}))
        db.commit()
        app = FastAPI()
        app.include_router(audio.router)
        app.dependency_overrides[get_db] = lambda: db
        share_session(monkeypatch, db)
        auth = audio.audio_transcription.__defaults__[0].dependency
        app.dependency_overrides[auth] = lambda: SimpleNamespace(user_id="owner", public_chat_visitor=None)
        # require_auth creates separate closures for each endpoint.
        status_auth = audio.audio_status.__defaults__[0].dependency
        app.dependency_overrides[status_auth] = app.dependency_overrides[auth]
        yield app, db
    engine.dispose()


def configure(db, kind="audio"):
    row = create_provider(db, display_name="ASR", provider_type=kind, provider="audio_compatible", base_url="http://asr.invalid/v1", api_key="private", model_name="test-asr")
    assign_role(db, "audio_transcription", row.provider_id)
    return row


def test_unconfigured_has_no_fallback_or_secrets(setup):
    app, db = setup
    with TestClient(app) as client:
        assert client.get("/v1/audio/status").json()["data"]["configured"] is False
        assert client.post("/v1/audio/transcriptions", content=wav(), headers={"content-type":"audio/wav"}).status_code == 503
        configure(db, "chat")
        assert resolve_audio_model(db) is None


def test_transcription_and_limits(setup, monkeypatch):
    app, db = setup
    row = configure(db)
    async def upstream(model, data):
        assert model.api_key == "private"
        assert data[:4] == b"RIFF"
        return "你好 HugAgentOS"
    monkeypatch.setattr(audio, "transcribe", upstream)
    with TestClient(app) as client:
        assert client.get("/v1/audio/status").json()["data"] == {"allowed":True,"configured":True,"max_seconds":120}
        response = client.post("/v1/audio/transcriptions", content=wav(), headers={"content-type":"audio/wav"})
        assert response.json()["data"]["text"] == "你好 HugAgentOS"
        assert "private" not in response.text
        assert client.post("/v1/audio/transcriptions", content=wav()).status_code == 415
        for body, status in [(b"bad",422),(b"x"*(audio.MAX_BYTES+1),413),(wav(121),413)]:
            assert client.post("/v1/audio/transcriptions", content=body, headers={"content-type":"audio/wav"}).status_code == status
        update_provider(db, row.provider_id, is_active=False)
        assert client.get("/v1/audio/status").json()["data"]["configured"] is False


def test_authentication_is_required(setup, monkeypatch):
    app, _ = setup
    app.dependency_overrides = {get_db: app.dependency_overrides[get_db]}
    # Exercise the real auth dependency in session mode; mock mode intentionally trusts localhost.
    from core.auth import backend
    monkeypatch.setattr(backend, "_auth_mode", lambda: "session")
    with TestClient(app) as client:
        assert client.get("/v1/audio/status").status_code in (401,403)
        assert client.post("/v1/audio/transcriptions", content=wav()).status_code in (401,403)


def test_admission_is_bounded_and_releases_on_failure():
    with pytest.raises(ValueError):
        with audio.transcription_slot("owner"):
            with pytest.raises(HTTPException) as error:
                with audio.transcription_slot("owner"): pass
            assert error.value.status_code == 429
            raise ValueError()
    assert not audio._active
    with audio.transcription_slot("1"), audio.transcription_slot("2"), audio.transcription_slot("3"), audio.transcription_slot("4"):
        with pytest.raises(HTTPException):
            with audio.transcription_slot("5"): pass


@pytest.mark.asyncio
async def test_disconnect_cancels_upstream(setup, monkeypatch):
    app, db = setup
    configure(db)
    cancelled = asyncio.Event()
    async def upstream(*_):
        try: await asyncio.sleep(30)
        finally: cancelled.set()
    monkeypatch.setattr(audio, "transcribe", upstream)
    async def disconnected(_): await asyncio.sleep(0.01)
    monkeypatch.setattr(audio, "_until_disconnect", disconnected)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/v1/audio/transcriptions", content=wav(), headers={"content-type":"audio/wav"})
    assert result.status_code == 499
    assert cancelled.is_set() and not audio._active


def test_voice_is_opt_in_and_revocation_is_immediate(setup, monkeypatch):
    app, db = setup
    configure(db)
    owner = db.get(UserShadow, "owner")
    async def must_not_call(*args):
        pytest.fail("unauthorized audio must not reach the model")
    monkeypatch.setattr(audio, "transcribe", must_not_call)
    with TestClient(app) as client:
        for metadata in ({}, {"role":"super_admin"}, {"can_use_voice":False}):
            owner.extra_data = metadata
            db.commit()
            assert client.get("/v1/audio/status").json()["data"] == {"allowed":False,"configured":False,"max_seconds":120}
            assert client.post("/v1/audio/transcriptions",content=wav(),headers={"Content-Type":"audio/wav"}).status_code == 403
        owner.extra_data = {"can_use_voice":True}
        db.commit()
        assert client.get("/v1/audio/status").json()["data"]["allowed"] is True
        owner.extra_data = {"can_use_voice":False}
        db.commit()
        assert client.post("/v1/audio/transcriptions",content=wav(),headers={"Content-Type":"audio/wav"}).status_code == 403


def test_public_visitor_does_not_inherit_owner_voice_permission(setup):
    app, db = setup
    configure(db)
    for endpoint in (audio.audio_status,audio.audio_transcription):
        app.dependency_overrides[endpoint.__defaults__[0].dependency] = lambda: SimpleNamespace(user_id="owner",public_chat_visitor="visitor")
    with TestClient(app) as client:
        assert client.get("/v1/audio/status").json()["data"]["allowed"] is False
        assert client.post("/v1/audio/transcriptions",content=wav(),headers={"Content-Type":"audio/wav"}).status_code == 403
