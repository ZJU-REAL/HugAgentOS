"""Isolated real HTTP fixture. Real routes/policy/DB; AUTH_MODE=mock only here.

The runner must supply isolated DATABASE_URL, STORAGE_PATH, TMPDIR and CONFIG_TOKEN.
Never run against a business database. There is deliberately no production lifespan.
"""

import os
from pathlib import Path

from api.routes.v1.content import router as content_router
from features.workbench.api.workbench import router
from core.db import models  # noqa: F401
from core.db.engine import Base, engine
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

if (
    "workbench-03" not in os.environ["DATABASE_URL"]
    or "workbench-03" not in os.environ["STORAGE_PATH"]
):
    raise RuntimeError("Expected workbench-03 isolated data directories")
Base.metadata.create_all(engine)
app = FastAPI()
app.include_router(router, prefix="/api")
app.include_router(content_router, prefix="/api")


@app.get("/destination")
def destination():
    return Response("<h1>External reports</h1>", media_type="text/html")


@app.get("/ready")
def ready():
    return {"ready": True}


app.mount(
    "/",
    StaticFiles(directory=Path("src/frontend/node_modules/.tmp/external-browser"), html=True),
    name="fixture",
)
