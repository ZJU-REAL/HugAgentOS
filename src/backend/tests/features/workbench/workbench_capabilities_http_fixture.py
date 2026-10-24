"""WB09 real HTTP fixture. No upstream or storage mocks; isolated scratch data only."""

import io
import json
import os
import zipfile

from api.routes.v1.artifacts import router as artifacts
from api.routes.v1.plugins import router as plugins
from features.workbench.api.workbench import router as workbench
from core.db.engine import Base, SessionLocal, engine
from core.db.models import SystemConfig, UserShadow
from core.infra.exceptions import AppException
from core.files.artifacts.service import store_bytes_as_artifact
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

if (
    "workbench-09" not in os.environ["DATABASE_URL"]
    or "workbench-09" not in os.environ["STORAGE_PATH"]
):
    raise RuntimeError("WB09 fixture requires isolated workbench-09 database and storage")
Base.metadata.create_all(engine)
with SessionLocal() as db:
    for uid in ["wb09", "other"]:
        if not db.get(UserShadow, uid):
            db.add(UserShadow(user_id=uid, username=uid, extra_data={"can_import_plugin": True}))
    db.commit()

app = FastAPI()
app.include_router(workbench, prefix="/api")
app.include_router(plugins, prefix="/api")
app.include_router(artifacts, prefix="/api")


def package(html, upstream):
    grants = [
        "data.query",
        "data_source:rows",
        "file.pick",
        "file.save",
        "chat.compose",
        "navigation.open",
    ]
    manifest = {
        "name": "wb09-capabilities",
        "version": "1.0.0",
        "extensions": {
            "org.hugagent": {
                "admin_config": {
                    "fields": [
                        {"key": "wb09.upstream", "label": "Fixture upstream", "type": "string"}
                    ]
                },
                "ui": {
                    "version": 3,
                    "contributes": {
                        "modules": [
                            {
                                "id": "page",
                                "surface": "workspace",
                                "entry": "web/index.html",
                                "title": "Capabilities",
                                "grants": grants,
                            }
                        ],
                        "data_sources": [
                            {
                                "id": "rows",
                                "url": "{config.wb09.upstream}/upstream/rows",
                                "method": "POST",
                                "params_schema": {},
                            }
                        ],
                        "applications": [
                            {
                                "schemaVersion": "1.0.0",
                                "id": "capabilities",
                                "title": "WB09 capabilities",
                                "target": {"kind": "plugin", "moduleId": "page"},
                            }
                        ],
                    },
                },
            }
        },
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        archive.writestr("web/index.html", html)
    return output.getvalue()


@app.post("/fixture/session")
async def fixture_session():
    from core.auth.session import create_session
    from core.config.settings import settings

    token = await create_session({"user_id": "wb09", "user_center_id": "wb09", "username": "wb09"})
    response = JSONResponse({"fixture": True})
    response.set_cookie(
        settings.session.cookie_name, token, httponly=True, samesite="lax", secure=False
    )
    return response


@app.post("/fixture/setup")
def setup(request: Request):
    with SessionLocal() as db:
        row = db.get(SystemConfig, "wb09.upstream")
        if not row:
            row = SystemConfig(
                config_key="wb09.upstream",
                display_name="WB09 fixture upstream",
                group_key="fixture",
                is_secret=False,
            )
            db.add(row)
        row.config_value = str(request.base_url).rstrip("/")
        row.updated_by = "fixture_admin"
        db.commit()
        own = store_bytes_as_artifact(
            db,
            user_id="wb09",
            content=b"WB09 real file bytes",
            filename="wb09-real-file.txt",
            mime_type="text/plain",
        )
        own_id = own.artifact_id
        other = store_bytes_as_artifact(
            db,
            user_id="other",
            content=b"Other owner bytes",
            filename="other-private-file.txt",
            mime_type="text/plain",
        )
        other_id = other.artifact_id
    from core.config.store.system_config import SystemConfigService

    SystemConfigService.get_instance().invalidate_cache()
    return {
        "fileId": own_id,
        "otherFileId": other_id,
        "upstream": str(request.base_url).rstrip("/"),
    }


@app.post("/fixture/package")
async def fixture_package(request: Request):
    body = await request.json()
    return Response(
        package(body["html"], str(request.base_url).rstrip("/")), media_type="application/zip"
    )


@app.post("/upstream/rows")
def upstream_rows():
    return {"rows": [{"id": 7, "name": "Real HTTP fixture row"}]}


@app.exception_handler(AppException)
async def app_error(request, error):
    return JSONResponse({"code": error.code}, status_code=error.status_code)


@app.get("/ready")
def ready():
    return {"ready": True}


@app.get("/apps/{path:path}")
@app.get("/settings")
def host_route(path=""):
    return FileResponse(os.path.join(os.environ["WB09_FRONTEND"], "index.html"))


app.mount("/", StaticFiles(directory=os.environ["WB09_FRONTEND"], html=True))
