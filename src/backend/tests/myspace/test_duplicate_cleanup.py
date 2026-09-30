"""Cleanup removes every member of duplicate groups and preserves recovery information."""
from datetime import datetime
import pytest
import sqlalchemy as sa
from core.myspace.duplicate_cleanup import duplicate_rows, clean_duplicates, restore_cleanup


@pytest.fixture
def legacy(tmp_path):
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    metadata = sa.MetaData()
    table = sa.Table("artifacts", metadata,
        sa.Column("artifact_id", sa.String, primary_key=True),
        sa.Column("user_id", sa.String),
        sa.Column("user_folder_id", sa.String),
        sa.Column("team_id", sa.String),
        sa.Column("filename", sa.String),
        sa.Column("storage_key", sa.String),
        sa.Column("updated_at", sa.DateTime),
        sa.Column("deleted_at", sa.DateTime))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), [
            dict(artifact_id=i,user_id=u,user_folder_id=f,team_id=t,
                 filename=n,storage_key=i,updated_at=datetime(2020,1,1))
            for i,u,f,t,n in [
                ("a","u",None,None,"same.txt"),("b","u",None,None,"same.txt"),
                ("c","u","folder",None,"same.txt"),("d","u","folder",None,"same.txt"),
                ("keep","u",None,None,"keep.txt"),("other","v",None,None,"same.txt"),
                ("team","u",None,"team","same.txt")
            ]])
    yield engine, table
    engine.dispose()


def test_preview_apply_and_restore(legacy):
    engine, table = legacy
    with engine.begin() as conn:
        assert len(duplicate_rows(conn)) == 4
        assert conn.execute(sa.select(sa.func.count()).select_from(table).where(
            table.c.deleted_at.is_(None))).scalar() == 7
        journal = clean_duplicates(conn)
        assert {r["artifact_id"] for r in journal["rows"]} == {"a","b","c","d"}
        assert duplicate_rows(conn) == []
        assert set(conn.execute(sa.select(table.c.artifact_id).where(
            table.c.deleted_at.is_(None))).scalars()) == {"keep","other","team"}
        assert clean_duplicates(conn)["rows"] == []
        restore_cleanup(conn, journal)
        assert len(duplicate_rows(conn)) == 4


def test_migration_requires_cleanup_and_raw_sql_remains_unique(legacy):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from fastapi import HTTPException

    path = Path(__file__).parents[2] / "alembic/versions/personalname01_unique_personal_filenames.py"
    spec = importlib.util.spec_from_file_location("personal_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine, table = legacy
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="duplicate names"):
                migration.upgrade()
            journal = clean_duplicates(conn)
            migration.upgrade()
            conn.execute(table.insert().values(
                artifact_id="replacement", user_id="u", filename="same.txt"))
            with pytest.raises((sa.exc.IntegrityError, HTTPException)):
                conn.execute(table.insert().values(
                    artifact_id="duplicate", user_id="u", filename="same.txt"))
            migration.downgrade()
            conn.execute(table.delete().where(table.c.artifact_id == "replacement"))
            restore_cleanup(conn, journal)
            assert len(duplicate_rows(conn)) == 4
