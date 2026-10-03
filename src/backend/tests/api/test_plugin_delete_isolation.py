"""Foreign local plugin removal returns a concealed 404, never a server error."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.routes.v1 import plugins
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.exceptions import ResourceNotFoundError
from fastapi.responses import JSONResponse


def test_foreign_local_plugin_delete_is_not_found(monkeypatch, db_session):
    def foreign(*args):
        raise PermissionError("not owned")

    monkeypatch.setattr("core.plugins.local.service.get", foreign)
    app = FastAPI()
    app.include_router(plugins.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="other", user_center_id="other", username="Other"
    )
    app.dependency_overrides[get_db] = lambda: db_session

    @app.exception_handler(ResourceNotFoundError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"message": exc.message})

    with TestClient(app) as client:
        response = client.delete("/v1/plugins/installed/plugin:local:owner-plugin")
    assert response.status_code == 404
    assert "not owned" not in response.text
