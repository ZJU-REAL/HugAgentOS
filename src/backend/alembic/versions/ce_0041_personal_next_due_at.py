"""Personal Agent: the earliest due time of each agent, indexed.

The scheduler read every agent's whole config every 15 seconds to find the due schedules.
`personal_agents.next_due_at` holds the earliest of an agent's schedule times and its
proactive check time (personal_agent.scheduling.schedules.store keeps it), so the due
agents are found by an index. Proactive checks use the same scheduler; they have no loop
of their own.

Same schema as the EE revision ``paproactive01``.

Revision ID: ce_0041
Revises: ce_0040
"""
from alembic import op
import sqlalchemy as sa

revision = "ce_0041"
down_revision = "ce_0040"
branch_labels = None
depends_on = None

TABLE, COLUMN, INDEX = "personal_agents", "next_due_at", "ix_personal_agents_next_due_at"
BATCH = 500


def due_at(config):
    """The same rule as personal_agent.scheduling.schedules.due_at when this revision was written."""
    config = config if isinstance(config, dict) else {}
    times = [item["next_run"] for item in config.get("schedules") or []
             if isinstance(item, dict) and item.get("enabled", True) and item.get("next_run")]
    beat = config.get("heartbeat") or {}
    if (beat.get("proactive") or {}).get("seeded") and beat.get("next_run"): times.append(beat["next_run"])
    return min(times) if times else None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names(): return
    if COLUMN not in {column["name"] for column in inspector.get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column(COLUMN, sa.Float(), nullable=True))
    if INDEX not in {index["name"] for index in inspector.get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, [COLUMN])
    # Existing schedules must stay due: fill the column from each agent's config, in batches by id.
    agents = sa.table(TABLE, sa.column("id", sa.String), sa.column("config", sa.JSON), sa.column(COLUMN, sa.Float))
    cursor = ""
    while True:
        rows = bind.execute(sa.select(agents.c.id, agents.c.config).where(agents.c.id > cursor)
                            .order_by(agents.c.id).limit(BATCH)).all()
        for ident, config in rows:
            when = due_at(config)
            if when is not None:
                bind.execute(agents.update().where(agents.c.id == ident).values({COLUMN: when}))
        if len(rows) < BATCH: break
        cursor = rows[-1][0]


def downgrade():
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
