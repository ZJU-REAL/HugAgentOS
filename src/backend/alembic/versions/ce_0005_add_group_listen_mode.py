"""Add channel_connections.group_listen_mode (group listening switch).

Revision ID: ce_0005
Revises: ce_0004
Create Date: 2026-08-08

Conditional on purpose. ``ce_0001`` builds the schema with ``ce_create_all`` straight from the
SQLAlchemy models, so a **fresh** CE install already has this column by the time the chain
reaches here — an unconditional ``add_column`` would fail on every new deployment. Only a CE
database created before the column existed needs the ALTER.
"""

import sqlalchemy as sa
from alembic import op

revision = "ce_0005"
down_revision = "ce_0004"
branch_labels = None
depends_on = None

_TABLE = "channel_connections"
_COLUMN = "group_listen_mode"


def _columns(bind) -> set:
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind)
    if not existing or _COLUMN in existing:
        return  # fresh install (already created by ce_create_all) or table absent
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN, sa.String(length=16), nullable=False, server_default="mention_only"
        ),
    )
    # SQLite cannot add a CHECK constraint to an existing table; PostgreSQL can.
    if bind.dialect.name != "sqlite":
        op.create_check_constraint(
            "channel_connections_group_listen_check",
            _TABLE,
            "group_listen_mode IN ('mention_only', 'observe_all')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _COLUMN not in _columns(bind):
        return
    if bind.dialect.name != "sqlite":
        op.drop_constraint(
            "channel_connections_group_listen_check", _TABLE, type_="check"
        )
    op.drop_column(_TABLE, _COLUMN)
