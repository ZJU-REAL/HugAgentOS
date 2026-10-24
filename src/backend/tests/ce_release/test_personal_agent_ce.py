"""Personal Agent must work in the physically derived community tree."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from core.db.personal_schema import ce_metadata
from core.db.edition import ce_create_all
from core.db.models import UserShadow
from personal_agent.models import PersonalDevice
from core.harness.registry import registry


def migration(connection):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/ce_0028_personal_agent.py"
    spec = importlib.util.spec_from_file_location("personal_agent_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.mark.parametrize("fresh", [True, False])
def test_ce_personal_agent_upgrade_preserves_existing_data_and_refuses_loss(tmp_path, fresh):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ce.db'}")
    with engine.begin() as connection:
        revision = migration(connection)
        metadata = ce_metadata()
        if fresh:
            ce_create_all(connection)
        else:
            metadata.create_all(connection, tables=[
                table for name, table in metadata.tables.items() if name not in revision.TABLES
            ])
        connection.execute(UserShadow.__table__.insert().values(
            user_id="owner", user_center_id="owner", username="Owner"))
        revision.upgrade()
        assert set(revision.TABLES) <= set(sa.inspect(connection).get_table_names())
        connection.execute(PersonalDevice.__table__.insert().values(
            id="device", user_id="owner", name="Computer", platform="linux", secret_hash="hash"))
        revision.upgrade()
        assert connection.execute(sa.select(PersonalDevice.id)).scalar_one() == "device"
        assert connection.execute(sa.select(UserShadow.username)).scalar_one() == "Owner"
        with pytest.raises(RuntimeError, match="data exists"):
            revision.downgrade()
        assert set(revision.TABLES) <= set(sa.inspect(connection).get_table_names())
        connection.execute(PersonalDevice.__table__.delete())
        revision.downgrade()
        assert not set(revision.TABLES) & set(sa.inspect(connection).get_table_names())
        assert connection.execute(sa.select(UserShadow.username)).scalar_one() == "Owner"
    engine.dispose()


def test_ce_session_serialization_exposes_personal_agent_without_license(tmp_path, monkeypatch):
    from api.routes.v1.auth import _serialize_user
    assert importlib.util.find_spec("edition_ee") is None
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    ce_create_all(engine)
    monkeypatch.setattr(registry.registration_state(), 'enabled', set())
    with Session(engine) as db:
        db.add(UserShadow(user_id="owner", user_center_id="owner", username="Owner"))
        db.commit()
        assert not _serialize_user(db, {"user_id": "owner"})["can_use_personal_agent"]
        registry.mark_enabled("personal_agent")
        assert _serialize_user(db, {"user_id": "owner"})["can_use_personal_agent"]
        assert not _serialize_user(db, {"user_id": "missing"})["can_use_personal_agent"]
    engine.dispose()
