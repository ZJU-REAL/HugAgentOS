"""Isolated browser/HTTP host using production routes and session authentication.

Run only with disposable DATABASE_URL and STORAGE_PATH; no application workers
or external-model calls are started. Fixture login is local to this test host.
"""

from contextlib import asynccontextmanager
from pathlib import Path
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


def build_app():
    root = Path(os.environ["CORE_E2E_ROOT"]).resolve()
    assert str(root).startswith("/tmp/"), "E2E data must be disposable"
    assert os.environ["DATABASE_URL"] == "sqlite:///" + str(root / "app.db")
    assert os.environ["STORAGE_PATH"] == str(root / "storage")
    assert os.environ.get("STORAGE_TYPE") == "local"
    assert os.environ.get("SESSION_STORE") == "memory"
    assert os.environ.get("REDIS_URL") == ""
    assert os.environ.get("AUTH_MODE") == "session"
    if os.environ.get("CORE_E2E_PROFILE") == "cloud":
        assert not os.environ.get("HUGAGENT_CAPS_ROOT")
    else:
        assert os.environ.get("HUGAGENT_CAPS_ROOT") == str(root / "caps")
    from core.db.engine import Base, engine, SessionLocal
    from core.db.models import UserShadow
    from core.auth.session import create_session
    from core.config.settings import settings
    from api.app import app as production_api

    assert settings.storage.type == "local"
    assert settings.storage.root.resolve() == root / "storage"
    assert settings.session.store_type == "memory"
    assert not settings.redis.url
    assert settings.auth.mode == "session"
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for uid in ("core-owner", "core-other"):
            if db.get(UserShadow, uid) is None:
                db.add(UserShadow(user_id=uid, username=uid, user_center_id=uid))
        db.commit()

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(lifespan=lifespan)

    @app.post("/__test/login/{identity}")
    async def login(identity: str):
        if identity not in ("core-owner", "core-other"):
            raise HTTPException(404)
        token = await create_session(
            {"user_id": identity, "user_center_id": identity, "username": identity}
        )
        response = JSONResponse({"ok": True})
        response.set_cookie(settings.session.cookie_name, token, httponly=True)
        return response

    @app.post("/__test/history/{chat_id}")
    def seed_history(chat_id: str):
        from uuid import uuid4
        from core.db.models import ChatSession, ChatMessage

        with SessionLocal() as db:
            chat = db.get(ChatSession, chat_id)
            if chat is None or chat.user_id != "core-owner":
                raise HTTPException(404)
            db.add(
                ChatMessage(
                    message_id=str(uuid4()),
                    chat_id=chat_id,
                    role="assistant",
                    content="Persisted core refactor history",
                )
            )
            db.commit()
        return {"ok": True}

    # Mounted app lifespan deliberately excludes production workers. Requests
    # still execute its real middleware, auth, routing, services and persistence.
    app.mount("/api", production_api)
    dist = Path("src/frontend/dist").resolve()
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def index(path: str):
        candidate = (dist / path).resolve()
        if candidate.is_relative_to(dist) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    return app
