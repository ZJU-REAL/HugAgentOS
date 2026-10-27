"""HTTP boundary for ephemeral local client tool sessions."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import ChatSession, Project
from integrations.agent_client import api
from tests.db_support import share_session

@pytest.fixture
def client(db_session, monkeypatch):
    db_session.add(Project(project_id='p',name='test',owner_user_id='u',kind='local',extra_data={'local':{'path':'/tmp'}}))
    db_session.add_all([ChatSession(chat_id='mine', user_id='u', title='mine', project_id='p'), ChatSession(chat_id='other', user_id='v', title='other')])
    db_session.commit()
    share_session(monkeypatch, db_session)
    app = FastAPI(); app.include_router(api.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id='u', user_center_id='u', username='test')
    monkeypatch.setattr(api, 'local_mode_enabled', lambda: True)
    with TestClient(app) as c: yield c

def test_lease_is_local_owner_bound_and_explicitly_released(client, monkeypatch):
    body = {'mode': 'ask', 'servers': {}, 'cwd': '/tmp'}
    assert client.post('/v1/client-sessions/other', json=body).status_code == 404
    response = client.post('/v1/client-sessions/mine', json=body)
    assert response.status_code == 200
    lease = response.json()['data']['lease_id']
    assert client.post('/v1/client-sessions/mine', json=body).status_code == 409
    assert client.patch('/v1/client-sessions/mine', json={**body, 'lease_id': '0' * 32}).status_code == 409
    assert client.patch('/v1/client-sessions/mine', json={**body, 'lease_id': lease, 'mode': 'full'}).status_code == 200
    assert client.delete('/v1/client-sessions/mine', params={'lease_id': lease}).status_code == 200
    monkeypatch.setattr(api, 'local_mode_enabled', lambda: False)
    assert client.post('/v1/client-sessions/mine', json=body).status_code == 403


def test_registration_cannot_bind_an_unrelated_working_directory(client):
    assert client.post('/v1/client-sessions/mine', json={'cwd':'/','mode':'ask'}).status_code == 409


def test_expired_lease_cannot_be_revived(client, monkeypatch):
    import time
    monkeypatch.setattr(api.registry, 'TTL_SECONDS', 0.05)
    first=client.post('/v1/client-sessions/mine',json={'cwd':'/tmp','mode':'ask'})
    lease=first.json()['data']['lease_id']
    time.sleep(0.15)
    assert client.patch('/v1/client-sessions/mine',json={'lease_id':lease,'mode':'full'}).status_code == 409
    second=client.post('/v1/client-sessions/mine',json={'cwd':'/tmp','mode':'ask'})
    assert second.status_code == 200
    assert second.json()['data']['lease_id'] != lease


def test_scoped_credentials_cannot_register_client_tools(client):
    client.app.dependency_overrides[get_current_user] = lambda: UserContext(user_id='u',user_center_id='u',username='test',api_key_project_id='p')
    assert client.post('/v1/client-sessions/mine',json={'cwd':'/tmp'}).status_code == 403


@pytest.mark.asyncio
async def test_failed_cancellation_keeps_revocation_fail_closed(monkeypatch):
    from integrations.agent_client import registry
    from core.runs import runtime
    chat = 'revocation-failure-test'
    lease_id = registry.create('u', chat, '/tmp', 'full', {})
    registry.bind_run('u', chat, lease_id, 'run-test', 'full')
    async def fail(*args, **kwargs):
        raise RuntimeError('cancellation unavailable')
    monkeypatch.setattr(runtime, 'cancel_run', fail)
    try:
        with pytest.raises(RuntimeError):
            await registry.revoke('u', chat, lease_id)
        with pytest.raises(ValueError):
            registry.require('u', chat, lease_id)
        with pytest.raises(ValueError):
            registry.create('u', chat, '/tmp', 'full', {})
    finally:
        registry._entries.pop(chat, None)
