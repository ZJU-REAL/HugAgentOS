"""Cold SQLite initialization must allow catalog configuration to seed independently."""
from core.config.store import system_config
from core.config.store import system_config_constants
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.db.engine import Base
from core.db.models import ChatSession, SystemConfig, UserShadow
from personal_agent.identity.service import IdentityService


def test_first_identity_allows_catalog_seed_on_another_connection(tmp_path, monkeypatch):
    from core.capabilities.catalog import resolver as catalog_resolver
    from core.config.store import system_config
    from personal_agent.identity import capabilities

    # Separate physical connections expose SQLite's writer lock; an in-memory
    # StaticPool would silently reuse the identity transaction and hide the bug.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cold-start.db'}",
        connect_args={"timeout": 0.05},
        poolclass=NullPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    config = system_config.SystemConfigService()
    monkeypatch.setattr("core.db.system_configuration.SessionLocal", factory)
    monkeypatch.setattr(system_config_constants, 'SEED_CONFIGS', [
        ("pa.cold_start", "seeded", "Cold start", "", "test", False),
    ])
    monkeypatch.setattr(capabilities, "plugin_inventory", lambda db, user_id: [])

    def resolve_catalog(db, user_id):
        # The real config getter lazily seeds through its own session. It must
        # finish while ensure() is still running, before the identity commits.
        assert config.get("pa.cold_start") == "seeded"
        with factory() as other:
            assert other.scalar(select(SystemConfig.config_value)) == "seeded"
        return [], [], []

    monkeypatch.setattr(catalog_resolver, "resolve_all_runtime_enabled", resolve_catalog)
    try:
        with factory() as db:
            db.add(UserShadow(user_id="owner", user_center_id="owner", username="Owner"))
            db.commit()
            identity = IdentityService(SimpleNamespace(db=db, user_id="owner", chat_id=None))
            created = identity.ensure()
            assert identity.ensure()["id"] == created["id"]
            assert db.scalar(select(ChatSession.chat_id)) == created["chat_id"]
    finally:
        engine.dispose()
