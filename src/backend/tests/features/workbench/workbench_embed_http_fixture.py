"""Real workbench routes with isolated persistence, without production lifespan."""

import os
from pathlib import Path

from features.workbench.api.workbench import router
from core.db import models  # noqa: F401
from core.db.engine import Base, engine
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

for name in ("DATABASE_URL", "STORAGE_PATH", "WB14_BROWSER_OUTPUT"):
    if "workbench-14" not in os.environ.get(name, ""):
        raise RuntimeError("Expected WB14 isolated paths")

Base.metadata.create_all(engine)
app = FastAPI()
app.include_router(router, prefix="/api")


@app.middleware("http")
async def isolation_headers(request, call_next):
    response = await call_next(request)
    if request.url.path != "/unprotected":
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    return response


@app.get("/ready")
def ready():
    return {"ready": True}


@app.get("/unprotected")
def unprotected():
    return HTMLResponse((Path(os.environ["WB14_BROWSER_OUTPUT"]) / "index.html").read_text())


app.mount("/", StaticFiles(directory=Path(os.environ["WB14_BROWSER_OUTPUT"]), html=True))
