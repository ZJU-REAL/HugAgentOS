"""Isolated E2E backend: real tool handlers, SQLite registry, catalog and plugin routes."""
import os
import sys
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="manager-e2e-"))
os.environ["HUGAGENT_CAPS_ROOT"] = str(home / "capabilities")
os.environ["DATABASE_URL"] = "sqlite:///" + str(home / "test.db")
os.environ["HUGAGENT_HOME"] = str(home)
os.environ["LOGIN_MODE"] = "local"

import asyncio
import json
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch
from core.db.engine import Base, get_db
from core.db import models
from core.capabilities import registry
from core.auth.backend import UserContext, get_current_user
from api.routes.v1 import catalog, plugins, me_capabilities
from core.plugins.local import service as local_plugin_service
from core.capabilities.local_plugin_runtime import configs
from core.llm.mcp_pool import make_client

engine = create_engine(os.environ["DATABASE_URL"], connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
factory = sessionmaker(bind=engine)
registry.SessionLocal = factory
user = "browser-owner"
with factory() as db:
    db.add(models.UserShadow(user_id=user, user_center_id=user, username="Browser owner"))
    db.commit()


def sessions():
    with factory() as db:
        yield db

app = FastAPI()
app.dependency_overrides[get_current_user] = lambda: UserContext(user_id=user, user_center_id=user, username="Browser owner")
app.dependency_overrides[get_db] = sessions
app.include_router(catalog.router, prefix="/api")
app.include_router(plugins.router, prefix="/api")
app.include_router(me_capabilities.router, prefix="/api")
# No network or installed global capabilities participate in this isolated test.
patch("core.auth.desktop_bridge.bridge_enabled", return_value=False).start()
patch.object(catalog, "get_runtime_catalog", return_value={"skills": [], "agents": [], "mcp": [], "kb": []}).start()
patch.object(catalog, "is_enabled", return_value=False).start()
patch.object(plugins.ps, "list_plugins", return_value=[]).start()

package_root = Path(__file__).resolve().parents[2] / "plugin_bundles/marketplace"
for manager in ("skill-manager", "plugin-manager"):
    local_plugin_service.install(user, str(package_root / manager))

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/test/tool/{manager}/{operation}")
async def tool(manager: str, operation: str, arguments: dict):
    server = next((sid, cfg) for sid, cfg in configs(user).items() if cfg.get("gateway_plugin") == manager)
    client = make_client(*server, is_stateful=False)
    handler = await client.get_tool(operation)
    result = await handler(**arguments)
    return json.loads(result.content[0].text)

@app.post("/test/package/{kind}")
def package(kind: str, body: dict):
    name = body["name"]
    if not name.replace("-", "").isalnum():
        raise ValueError("invalid name")
    folder = home / "inputs" / name
    skill = folder if kind == "skill" else folder / "skills" / "child-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name if kind == 'skill' else 'child-skill'}\ndescription: Browser lifecycle sample\n---\n{body.get('text', 'Version one')}")
    if kind == "plugin":
        (folder / "plugin.json").write_text(json.dumps({"name": name, "version": "1.0.0", "description": "Browser plugin sample"}))
    return {"path": str(folder)}

@app.get("/test/archive/{name}")
def archive_package(name: str):
    import io
    import zipfile
    from fastapi import Response
    if not name.replace("-", "").isalnum():
        raise ValueError("invalid name")
    root = home / "inputs" / name
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as package:
        for path in root.rglob("*"):
            if path.is_file():
                package.writestr(path.relative_to(root).as_posix(), path.read_bytes())
    return Response(data.getvalue(), media_type="application/zip")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
