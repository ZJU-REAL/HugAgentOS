"""Real HTTP/routes/storage fixture, with only authentication identity injected."""

import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from features.workbench.api.workbench import router as workbench
from api.routes.v1.plugins import router as plugins
from core.auth.backend import get_current_user, UserContext
from core.db.engine import Base, engine, SessionLocal
from core.db.models import UserShadow
from core.infra.exceptions import AppException

if (
    not any(marker in os.environ["DATABASE_URL"] for marker in ("workbench-07", "workbench-08"))
    or not any(marker in os.environ["STORAGE_PATH"] for marker in ("workbench-07", "workbench-08"))
):
    raise RuntimeError("Expected isolated workbench-07 data")
Base.metadata.create_all(engine)
with SessionLocal() as db:
    for uid in ["wb07", "other"]:
        if not db.get(UserShadow, uid):
            db.add(UserShadow(user_id=uid, username=uid, extra_data={"can_import_plugin": True}))
    db.commit()
app = FastAPI()
app.include_router(workbench, prefix="/api")
app.include_router(plugins, prefix="/api")


@app.post("/fixture/session")
@app.post("/fixture/session/{uid}")
async def fixture_session(uid: str = "wb07"):
    from core.auth.session import create_session
    from core.config.settings import settings

    if uid not in ("wb07", "other"):
        return JSONResponse({"error": "unknown fixture user"}, status_code=404)
    token = await create_session({"user_id": uid, "user_center_id": uid, "username": uid})
    response = JSONResponse({"fixture": True})
    response.set_cookie(
        settings.session.cookie_name, token, httponly=True, samesite="lax", secure=False
    )
    return response


@app.exception_handler(AppException)
async def app_error(request, error):
    return JSONResponse({"code": error.code}, status_code=error.status_code)


@app.get("/ready")
def ready():
    return {"ready": True}


app.mount("/", StaticFiles(directory=os.environ["WB07_FRONTEND"], html=True))
