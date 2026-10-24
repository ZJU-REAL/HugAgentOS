"""Stored destinations survive removal of the flat runtime representation."""
import pytest
from sqlalchemy import Column, JSON, MetaData, String, Table, create_engine, select
from features.automation.schema import upgrade_delivery_targets
from features.automation.delivery_targets import resolve_delivery_targets

@pytest.mark.parametrize("targets", [None, [], [{"type": "inapp"}],
                                    [{"type": "channel", "channel_id": "chosen", "conversation_id": "chosen-room"}]])
def test_migration_preserves_destination_precedence_and_metadata(targets):
    engine = create_engine("sqlite://")
    table = Table("scheduled_tasks", MetaData(), Column("task_id", String, primary_key=True),
                  Column("metadata", JSON))
    table.metadata.create_all(engine)
    data = {"channel_id": "old", "conversation_id": "room", "loop_id": "keep"}
    if targets is not None:
        data["delivery_targets"] = targets
    with engine.begin() as conn:
        conn.execute(table.insert().values(task_id="task", metadata=data))
        upgrade_delivery_targets(conn)
        first = conn.execute(select(table.c.metadata)).scalar_one()
        upgrade_delivery_targets(conn)
        assert conn.execute(select(table.c.metadata)).scalar_one() == first
        assert first["loop_id"] == "keep"
        assert "channel_id" not in first and "conversation_id" not in first
        expected = targets or [{"type": "inapp"}, {"type": "channel", "channel_id": "old", "conversation_id": "room"}]
        assert resolve_delivery_targets(first) == expected
    engine.dispose()


def test_runtime_does_not_interpret_flat_destinations():
    assert resolve_delivery_targets({"channel_id": "old", "conversation_id": "room"}) == [{"type": "inapp"}]
