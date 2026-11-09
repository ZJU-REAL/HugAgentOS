"""Create, rename and toggle a private knowledge base through the assembled app.

Runs in both editions (``scripts/ce_build_gates.py::pytest_check`` runs it in the
derived CE tree) because both historical failures were assembly-level, invisible
to a test that mounts only the knowledge-base router:

* ``PATCH /v1/catalog/kb/{kb_id}`` was caught by the generic catalog toggle route
  registered earlier, so every rename answered "At least one field must be provided";
* CE shipped its own copy of the request models without ``index_modes``, so the
  create/rename routes raised AttributeError (500) there.
"""

import pytest
from fastapi.testclient import TestClient

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import UserShadow
from features.knowledge.models import KBSpace

OWNER = "kb-rename-owner"


@pytest.fixture()
def client(db_session, monkeypatch):
    from api.app import app
    import core.auth.capabilities as capabilities

    db_session.add(UserShadow(user_id=OWNER, username="Owner"))
    db_session.commit()
    # Creation is gated by the role capability; this test is about the routes.
    monkeypatch.setattr(
        capabilities, "resolve_user_capabilities", lambda *_: {"can_create_private_kb": True}
    )
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=OWNER, user_center_id=OWNER, username="Owner"
    )
    app.dependency_overrides[get_db] = lambda: db_session
    # No lifespan (not a context manager): startup owns process-wide runtimes
    # bound to one event loop, and these routes need none of them.
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _create(client) -> str:
    response = client.post(
        "/v1/catalog/kb", json={"name": "Original", "visibility": "private", "index_modes": ["rag"]}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["kb_id"]


def test_rename_existing_private_kb(client, db_session):
    kb_id = _create(client)

    response = client.patch(f"/v1/catalog/kb/{kb_id}", json={"name": "Renamed"})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["name"] == "Renamed"
    db_session.expire_all()
    assert db_session.get(KBSpace, kb_id).name == "Renamed"


def test_editor_save_with_index_modes_renames_and_keeps_rag(client, db_session):
    kb_id = _create(client)

    # The catalog editor always sends the full form, index modes included.
    response = client.patch(
        f"/v1/catalog/kb/{kb_id}",
        json={"name": "知识库改名", "description": "", "index_modes": ["rag"]},
    )

    assert response.status_code == 200, response.text
    db_session.expire_all()
    space = db_session.get(KBSpace, kb_id)
    assert space.name == "知识库改名"
    assert space.extra_data["index_modes"] == ["rag"]


def test_kb_toggle_uses_its_own_url_and_leaves_the_space_untouched(client, db_session):
    kb_id = _create(client)

    response = client.patch(f"/v1/catalog/knowledge_bases/{kb_id}", json={"enabled": False})

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"kind": "kb", "id": kb_id, "enabled": False, "config": {}}
    db_session.expire_all()
    assert db_session.get(KBSpace, kb_id).name == "Original"
