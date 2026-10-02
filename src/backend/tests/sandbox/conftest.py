"""沙箱测试的共用前置。"""

import pytest
from core.infra.ephemeral import LocalEphemeralState
from core.sandbox import session_registry


@pytest.fixture(autouse=True)
def sandbox_session_store(monkeypatch):
    """会话↔容器的登记走进程内那份 TTL keyspace，单测不去连 Redis。

    两个 provider 现在都把这条绑定写进共享登记（见
    ``core/sandbox/session_registry.py``），所以任何碰会话生命周期的用例都会读写它。
    返回的这份 store 可以被用例直接拿去当"另一个进程看到的东西"检查。
    """
    monkeypatch.setenv("CUBE_OWNER_TAG", "")
    store = LocalEphemeralState()
    monkeypatch.setattr(session_registry, "get_ephemeral_state", lambda: store)
    return store


@pytest.fixture
def provider_database(db_session, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    sessions = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr("core.db.engine.SessionLocal", sessions)
    return sessions
