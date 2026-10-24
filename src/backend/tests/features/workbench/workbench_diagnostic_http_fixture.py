"""WB19 isolated real HTTP/SQLite fixture, never production lifespan."""

import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from features.workbench.api.workbench import router
from core.auth.backend import UserContext, get_current_user
from core.db.engine import Base, engine, SessionLocal
from core.db.models import UserShadow

for name in ("DATABASE_URL", "STORAGE_PATH", "WB19_ASSET_PATH"):
    if "workbench-19" not in os.environ[name]:
        raise RuntimeError("WB19 fixture needs isolated paths")
Base.metadata.create_all(engine)
with SessionLocal() as db:
    if db.get(UserShadow, "wb19-http") is None:
        db.add(UserShadow(user_id="wb19-http", username="HTTP reader", extra_data={}))
        db.commit()
app = FastAPI()
app.include_router(router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: UserContext(
    user_id="wb19-http", user_center_id="wb19-http", username="HTTP reader"
)


@app.get("/ready")
def ready():
    return {"ready": True}


app.mount("/", StaticFiles(directory=Path(os.environ["WB19_ASSET_PATH"]), html=True), name="assets")
