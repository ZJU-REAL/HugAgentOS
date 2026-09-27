"""The evaluation control plane is not an ordinary chat capability."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.auth.backend import UserContext, get_current_user


@pytest.fixture
def client(monkeypatch):
    from api.routes.v1 import evaluation_sandboxes as routes
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="eval-user", user_center_id="eval-user", username="Evaluator"
    )
    monkeypatch.setenv("CONFIG_TOKEN", "control-secret")
    with TestClient(app) as client:
        yield client


def test_chat_credentials_alone_cannot_create_sandbox(client):
    response = client.post("/v1/evaluation/sandboxes", json={"image": "ageval-pkg:test"})
    assert response.status_code == 403


def test_control_token_is_required_even_when_supplied_wrong(client):
    response = client.post(
        "/v1/evaluation/sandboxes", json={"image": "ageval-pkg:test"},
        headers={"X-Evaluation-Control": "wrong"},
    )
    assert response.status_code == 403
    assert "control-secret" not in response.text


def test_control_schema_rejects_host_mounts_and_arbitrary_images(client):
    response = client.post(
        "/v1/evaluation/sandboxes",
        json={"image": "ubuntu:latest", "volumes": ["/:/host"]},
        headers={"X-Evaluation-Control": "control-secret"},
    )
    assert response.status_code == 422
