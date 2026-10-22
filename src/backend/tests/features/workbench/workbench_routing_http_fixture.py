"""Isolated WB08 HTTP browser fixture with SPA fallback."""
import os
from pathlib import Path
from fastapi.responses import FileResponse
# Reuse the real plugin/session fixture, in a separate process and database.
from tests.features.workbench.workbench_plugin_http_fixture import app

@app.middleware("http")
async def frontend_history(request, call_next):
    if not request.url.path.startswith(("/api/", "/fixture/", "/ready")) and request.url.path not in ("/fixture.js",):
        return FileResponse(Path(os.environ["WB07_FRONTEND"]) / "index.html")
    return await call_next(request)
