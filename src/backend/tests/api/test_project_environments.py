from core.capabilities.skills import loader_factory as loader_factory_module
from api.routes.v1.project_environments import router
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import Project, UserShadow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.db_support import share_session


def test_environment_endpoint_enforces_project_permissions_and_empty_custom(
    db_session, monkeypatch
):
    from core.capabilities.skills import loader
    from core.db import engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(engine, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(loader_factory_module, 'global_loader', None)
    db_session.add_all(
        [
            UserShadow(user_id="owner", username="Owner"),
            UserShadow(user_id="outsider", username="Outsider"),
        ]
    )
    db_session.add(
        Project(project_id="project", name="Project", kind="personal", owner_user_id="owner")
    )
    db_session.commit()
    app = FastAPI()
    app.include_router(router)
    actor = {"id": "outsider"}
    app.dependency_overrides[get_db] = lambda: db_session
    share_session(monkeypatch, db_session)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=actor["id"], user_center_id=actor["id"], username=actor["id"]
    )
    with TestClient(app) as client:
        assert (
            client.put("/v1/projects/project/environment", json={"mode": "custom"}).status_code
            == 404
        )
        actor["id"] = "owner"
        response = client.put("/v1/projects/project/environment", json={"mode": "custom"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["effective"] == {
            "agent_ids": [],
            "plugin_ids": [],
            "skill_ids": [],
            "mcp_ids": [],
        }
        assert (
            client.get("/v1/projects/project/environment").json()["data"]["config"]["mode"]
            == "custom"
        )
        assert (
            client.put("/v1/projects/project/environment", json={"mode": "unknown"}).status_code
            == 422
        )
        assert (
            client.put(
                "/v1/projects/project/environment",
                json={
                    "mode": "custom",
                    "mcp_ids": ["not-installed"],
                },
            ).status_code
            == 422
        )
