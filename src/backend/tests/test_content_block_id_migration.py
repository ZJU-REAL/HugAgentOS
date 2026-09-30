"""Capability revision IDs fit both new schemas and upgraded databases."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from core.db.models import ContentBlock
from core.services.capability_workcopies import _id


def test_capability_record_ids_fit_model():
    for prefix in ("cap-cloud-copy:", "cap-cloud-version:", "cap-cloud-receipt:"):
        assert len(_id(prefix, "owner", "skill", "key")) <= ContentBlock.id.type.length


def test_upgrade_preserves_rows_and_downgrade_refuses_data_loss(tmp_path):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/capids01_widen_content_block_ids.py"
    if not path.exists():
        path = path.with_name("ce_0017_widen_content_block_ids.py")
    spec = importlib.util.spec_from_file_location("capids_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    metadata = sa.MetaData()
    table = sa.Table("content_blocks", metadata,
                     sa.Column("id", sa.String(64), primary_key=True),
                     sa.Column("payload", sa.JSON, nullable=False))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(id="docs_updates", payload={"preserved": True}))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert sa.inspect(conn).get_columns("content_blocks")[0]["type"].length == 128
        assert conn.execute(sa.select(table.c.payload)).scalar_one() == {"preserved": True}
        long_id = _id("cap-cloud-receipt:", "owner", "skill", "request")
        conn.execute(table.insert().values(id=long_id, payload={"receipt": True}))
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="longer than 64"):
                migration.downgrade()
        assert conn.execute(sa.select(table.c.id).where(table.c.id == long_id)).scalar_one() == long_id
        conn.execute(table.delete().where(table.c.id == long_id))
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert sa.inspect(conn).get_columns("content_blocks")[0]["type"].length == 64
        assert conn.execute(sa.select(table.c.payload)).scalar_one() == {"preserved": True}
    engine.dispose()
