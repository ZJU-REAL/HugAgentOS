"""Committed model changes must be visible to independently cached workers."""

from sqlalchemy.orm import sessionmaker

from core.models.repository import assign_role, create_provider, unassign_role, update_provider
from core.models import config as model_config
from core.models.config import ModelConfigService


def test_independent_workers_observe_committed_role_and_provider_changes(db_session, monkeypatch):
    monkeypatch.setattr(model_config, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    provider = create_provider(
        db_session,
        display_name="old",
        provider_type="chat",
        base_url="http://example.invalid",
        api_key="test",
        model_name="old",
        extra_config={"context_length": 8192},
    )
    assign_role(db_session, "subagent", provider.provider_id)
    workers = [ModelConfigService(), ModelConfigService()]
    for worker in workers:
        assert worker.resolve("subagent").model_name == "old"
        assert worker.resolve_provider(provider.provider_id).model_name == "old"
    update_provider(db_session, provider.provider_id, model_name="new")
    for worker in workers:
        assert worker.resolve("subagent").model_name == "new"
        assert worker.resolve_provider(provider.provider_id).model_name == "new"
    unassign_role(db_session, "subagent")
    for worker in workers:
        assert worker.resolve("subagent") is None
    update_provider(db_session, provider.provider_id, is_active=False)
    for worker in workers:
        assert worker.resolve_provider(provider.provider_id) is None
        assert worker.resolve_failover_chain(None) == []


def test_separate_process_observes_commit_without_restart(tmp_path):
    import os
    import subprocess
    import sys
    from sqlalchemy import create_engine
    from core.db.engine import Base
    import core.db.models

    url = f"sqlite:///{tmp_path / 'workers.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        provider = create_provider(
            db,
            display_name="old",
            provider_type="chat",
            base_url="http://example.invalid",
            api_key="test",
            model_name="old",
        )
        assign_role(db, "subagent", provider.provider_id)
        script = """
import sys
from core.models.config import ModelConfigService
service = ModelConfigService()
print(service.resolve('subagent').model_name, flush=True)
sys.stdin.readline()
print(service.resolve('subagent').model_name, flush=True)
sys.stdin.readline()
print(service.resolve('subagent'), flush=True)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            env=dict(os.environ, DATABASE_URL=url),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout.readline().strip() == "old"
            update_provider(db, provider.provider_id, model_name="new")
            child.stdin.write("next\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "new"
            unassign_role(db, "subagent")
            child.stdin.write("next\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "None"
            assert child.wait(timeout=10) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    engine.dispose()


def test_current_snapshot_is_served_while_another_thread_reloads(db_session, monkeypatch):
    import threading

    monkeypatch.setattr(model_config, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    provider = create_provider(
        db_session,
        display_name="main",
        provider_type="chat",
        base_url="http://example.invalid",
        api_key="test",
        model_name="main",
    )
    assign_role(db_session, "main_agent", provider.provider_id)
    service = ModelConfigService()
    assert service.resolve("main_agent").model_name == "main"

    # A reload in progress elsewhere must not hold up lookups whose revision matches.
    with service._refresh_lock:
        result = []
        reader = threading.Thread(target=lambda: result.append(service.resolve("main_agent")))
        reader.start()
        reader.join(timeout=5)
        assert not reader.is_alive()
    assert result[0].model_name == "main"


def test_context_length_falls_back_to_unassigned_providers(db_session, monkeypatch):
    monkeypatch.setattr(model_config, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    create_provider(
        db_session,
        display_name="spare",
        provider_type="chat",
        base_url="http://example.invalid",
        api_key="test",
        model_name="spare",
        extra_config={"context_length": 32768},
    )
    service = ModelConfigService()
    assert service.get_context_length_by_model_name("spare") == 32768
    assert service.get_context_length_by_model_name("missing") is None
