"""Schema upgrades for automation admission and canonical delivery targets."""
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

RUN_STATUSES = "status IN ('pending', 'running', 'success', 'failed')"


def upgrade_queue(connection):
    inspector = inspect(connection)
    if not inspector.has_table("scheduled_task_runs"):
        return
    current = next((item for item in inspector.get_check_constraints("scheduled_task_runs")
                    if item.get("name") == "scheduled_task_runs_status_check"), None)
    if current and "'pending'" in current["sqltext"]:
        return
    operations = Operations(MigrationContext.configure(connection))
    with operations.batch_alter_table("scheduled_task_runs") as batch:
        if current:
            batch.drop_constraint("scheduled_task_runs_status_check", type_="check")
        batch.create_check_constraint("scheduled_task_runs_status_check", RUN_STATUSES)


def initialize_schema():
    from core.db.engine import engine
    with engine.begin() as connection:
        upgrade_queue(connection)
        upgrade_delivery_targets(connection)


def upgrade_delivery_targets(connection):
    """Migrate stored flat destinations once; request handling reads only the new form."""
    from sqlalchemy import Column, JSON, MetaData, String, Table, select, text
    if not inspect(connection).has_table("scheduled_tasks"):
        return
    tasks = Table("scheduled_tasks", MetaData(),
                  Column("task_id", String), Column("metadata", JSON))
    predicate = (
        "metadata::jsonb ?| array['channel_id', 'conversation_id']"
        if connection.dialect.name == "postgresql" else
        "json_type(metadata, '$.channel_id') IS NOT NULL OR "
        "json_type(metadata, '$.conversation_id') IS NOT NULL"
    )
    query = select(tasks.c.task_id, tasks.c.metadata).where(text(predicate))
    for task_id, data in connection.execute(query):
        if not isinstance(data, dict) or not ({"channel_id", "conversation_id"} & data.keys()):
            continue
        data = dict(data)
        channel = data.pop("channel_id", None)
        conversation = data.pop("conversation_id", None)
        targets = data.get("delivery_targets")
        valid = [dict(item) for item in targets
                 if isinstance(item, dict) and item.get("type") in ("inapp", "channel", "email")] if isinstance(targets, list) else []
        if not valid:
            valid = [{"type": "inapp"}]
            if channel and conversation:
                valid.append({"type": "channel", "channel_id": channel, "conversation_id": conversation})
        data["delivery_targets"] = valid
        connection.execute(tasks.update().where(tasks.c.task_id == task_id).values(metadata=data))
