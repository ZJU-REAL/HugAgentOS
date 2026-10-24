"""Isolated WB-06 HTTP fixture; uses real workbench routes and SQLite."""
import os
from pathlib import Path

from features.workbench.api.workbench import router
from core.db.engine import Base, engine
from core.db.models import UserShadow
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

if "workbench-06" not in os.environ["DATABASE_URL"] or "workbench-06" not in os.environ["STORAGE_PATH"]:
    raise RuntimeError("Expected isolated WB-06 directories")
Base.metadata.create_all(engine)
with Session(engine) as db:
    if not db.get(UserShadow, "wb-reader"):
        db.add(UserShadow(user_id="wb-reader", user_center_id="wb-reader", username="Reader", extra_data={}))
        db.commit()
app = FastAPI()
app.include_router(router, prefix="/api")


@app.get("/ready")
def ready():
    return {"ready": True}


@app.get("/destination/{identifier}")
def destination(identifier: str):
    return Response("<h1>Report destination</h1>", media_type="text/html")


app.mount("/", StaticFiles(directory=Path("src/frontend/node_modules/.tmp/overflow-browser"), html=True))
