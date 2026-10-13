"""Desktop upgrades preserve existing data and live generated projections."""
from sqlalchemy import Boolean, Column, Computed, MetaData, String, Table, create_engine, text
from core.db.schema_reconcile import reconcile_metadata_schema


def test_existing_sqlite_generated_columns_upgrade_without_rebuilding_rows():
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE messages (id TEXT PRIMARY KEY, metadata JSON)"))
        db.execute(text("INSERT INTO messages VALUES ('existing', :metadata)"),
                   {"metadata": '{"history": true}'})
    metadata = MetaData()
    Table("messages", metadata, Column("id", String, primary_key=True),
          Column("metadata", String), Column("history", Boolean,
          Computed("coalesce(CAST(metadata ->> 'history' AS BOOLEAN), false)", persisted=True)))
    first = reconcile_metadata_schema(engine, metadata)
    assert first["columns"] == ["messages.history"]
    assert reconcile_metadata_schema(engine, metadata)["columns"] == []
    with engine.begin() as db:
        assert db.execute(text("SELECT id, history FROM messages")).one() == ("existing", 1)
        db.execute(text("UPDATE messages SET metadata = :metadata"),
                   {"metadata": '{"history": false}'})
        assert db.execute(text("SELECT history FROM messages")).scalar() == 0
