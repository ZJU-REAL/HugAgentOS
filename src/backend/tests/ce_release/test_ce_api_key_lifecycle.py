"""CE credential revocation fences shared durable run execution."""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import ChatRun, UserApiKey, UserShadow
from core.auth.api_key_runs import cancel_key_runs, require_admission_key
from core.runs.journal import RunJournal


def test_ce_revocation_cancels_only_bound_runs_and_blocks_admission(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'key-lifecycle.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    now = datetime.now(timezone.utc)
    with sessions() as db:
        db.add(UserShadow(user_id="owner", username="Owner"))
        db.add(UserApiKey(id="key", user_id="owner", name="Client",
                          key_prefix="test", key_hash="test", enabled=False))
        for run_id, payload in [("bound", {"api_key_id": "key"}), ("web", {})]:
            db.add(ChatRun(run_id=run_id, user_id="owner", chat_id=run_id,
                           message_id=run_id, status="pending", request_payload=payload))
        db.commit()
        cancel_key_runs(db, "key", "owner", now)
        db.commit()
        assert db.get(ChatRun, "bound").status == "cancelled"
        assert db.get(ChatRun, "web").status == "pending"
        with pytest.raises(HTTPException) as error:
            require_admission_key(db, {"api_key_id": "key"}, "owner", now)
        assert error.value.status_code == 401
    assert not RunJournal(sessions).claim("bound", owner="worker", lease_seconds=30)
    engine.dispose()


def test_ce_project_credentials_keep_scope_and_register_http_routes(tmp_path, monkeypatch):
    from api.app import app
    from core.chat.schemas import ChatRequest
    from core.auth.backend import UserContext
    from core.db.models import ChatSession, Project
    from core.auth.api_keys import ApiKeyService
    from core.projects.api import prepare_project_api_request

    paths = app.openapi()["paths"]
    assert "/v1/projects/{project_id}/api-keys" in paths
    assert "/v1/file/{file_id}/text" in paths
    monkeypatch.setattr("core.infra.crypto.encrypt_secret", lambda _: "encrypted")
    engine = create_engine(f"sqlite:///{tmp_path / 'project-credentials.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        db.add(UserShadow(user_id="owner", username="Owner"))
        db.add(Project(project_id="project", owner_user_id="owner", kind="personal", name="Project"))
        db.commit()
        key, _ = ApiKeyService(db).create_key("owner", "Client", project_id="project")
        assert ApiKeyService(db).list_keys("owner") == []
        user = UserContext(user_id="owner", user_center_id="owner", username="Owner",
                           api_key_id=key.id, api_key_project_id="project")
        request = prepare_project_api_request(db, user, ChatRequest(chat_id="api", message="Hi"))
        assert request.project_id == "project"
        assert db.get(ChatSession, "api").extra_data["project_api_scope"]["api_key_id"] == key.id
        with pytest.raises(HTTPException):
            prepare_project_api_request(db, user, ChatRequest(chat_id="other", message="Hi", project_id="outside"))
        assert db.get(ChatSession, "other") is None
    engine.dispose()


def test_ce_desktop_legacy_constraints_upgrade_preserves_rows(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from core.db.schema_reconcile import _patch_file_edit_constraints_sqlite

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-checks.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, size_bytes INTEGER CHECK (size_bytes > 0))"))
        connection.execute(text("CREATE TABLE user_api_keys (id TEXT PRIMARY KEY, agent_id TEXT, project_id TEXT)"))
        connection.execute(text("INSERT INTO artifacts VALUES ('existing', 7)"))
        connection.execute(text("INSERT INTO user_api_keys VALUES ('existing', 'agent', NULL)"))
        assert _patch_file_edit_constraints_sqlite(connection) == ["artifacts", "user_api_keys"]
        assert _patch_file_edit_constraints_sqlite(connection) == []
        connection.execute(text("INSERT INTO artifacts VALUES ('empty', 0)"))
        assert connection.execute(text("SELECT size_bytes FROM artifacts WHERE artifact_id='existing'")).scalar() == 7
        assert connection.execute(text("SELECT agent_id FROM user_api_keys WHERE id='existing'")).scalar() == "agent"
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO artifacts VALUES ('negative', -1)"))
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO user_api_keys VALUES ('conflict', 'agent', 'project')"))
    engine.dispose()
