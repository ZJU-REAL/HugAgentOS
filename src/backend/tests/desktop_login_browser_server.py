"""Isolated E2E fixture: real grant APIs, real compiled web UI, synthetic SSO session.

Never import from production. No application startup, real DB, or real credentials.
"""
import dataclasses
from pathlib import Path
import socket
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from core.config.settings import settings
from core.auth.session import create_session, validate_session
from core.infra.responses import success_response
from api.routes.v1.desktop_login import router

object.__setattr__(settings, "session", dataclasses.replace(settings.session, store_type="memory"))
app = FastAPI()
app.include_router(router, prefix="/api")
web = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.post("/__test__/session")
async def fixture_session(response: Response):
    token = await create_session({"user_id": "e2e-desktop", "username": "端到端测试账号",
                                  "email": "desktop@example.test"})
    response.set_cookie(settings.session.cookie_name, token, httponly=True, samesite="strict")
    return {"ok": True}


@app.get("/api/v1/auth/session/check")
async def session_check(request: Request):
    user = await validate_session(request.cookies.get(settings.session.cookie_name, ""))
    if not user:
        raise HTTPException(401)
    return success_response(data=user)


@app.get("/api/v1/meta/edition")
async def edition():
    return success_response(data={"edition": "ee", "features": {}})


app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")


@app.get("/")
async def index():
    return FileResponse(web / "index.html", headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    print(f"FIXTURE_URL http://127.0.0.1:{sock.getsockname()[1]}", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(sockets=[sock])
