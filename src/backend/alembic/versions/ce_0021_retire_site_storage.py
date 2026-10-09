"""Retire site KV and built-in submissions after a verified filesystem archive."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "ce_0021"
down_revision = "ce_0020"
branch_labels = None
depends_on = None
TABLES = ("site_kv", "site_submissions")


def upgrade():
    from core.db.site_storage_retirement import retire_connection

    directory = Path(
        os.environ.get(
            "SITE_STORAGE_BACKUP_DIR",
            str(Path(os.environ.get("STORAGE_PATH", "./storage")) / "migration-backups"),
        )
    )
    retire_connection(op.get_bind(), directory)


def downgrade():
    filename = os.environ.get("SITE_STORAGE_RESTORE_FILE")
    if not filename:
        raise RuntimeError("Set SITE_STORAGE_RESTORE_FILE to the verified retirement archive")
    manifest = json.loads(Path(filename).read_text(encoding="utf-8"))
    payload = manifest["tables"]
    from core.db.site_storage_retirement import _digest

    if manifest.get("revision") != "site05retire" or manifest.get("sha256") != _digest(payload):
        raise RuntimeError("Retired storage archive is invalid")
    if set(payload) - set(TABLES):
        raise RuntimeError("Unexpected tables in retirement archive")
    connection = op.get_bind()
    if any(sa.inspect(connection).has_table(name) for name in TABLES):
        raise RuntimeError("Retired tables already exist; refusing to overwrite")
    op.create_table(
        "site_kv",
        sa.Column(
            "site_id",
            sa.String(64),
            sa.ForeignKey("sites.site_id", name="fk_site_kv_site_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("k", sa.String(64), nullable=False),
        sa.Column("v", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("site_id", "k"),
    )
    op.create_table(
        "site_submissions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "site_id",
            sa.String(64),
            sa.ForeignKey("sites.site_id", name="fk_site_submissions_site_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("form_key", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("client_ip", sa.String(45)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "idx_site_submissions_site_created", "site_submissions", ["site_id", "created_at"]
    )
    for name, rows in payload.items():
        table = sa.Table(name, sa.MetaData(), autoload_with=connection)
        timestamp = "updated_at" if name == "site_kv" else "created_at"
        for row in rows:
            if row.get(timestamp):
                row[timestamp] = datetime.fromisoformat(row[timestamp])
        if rows:
            connection.execute(sa.insert(table), rows)
        if connection.scalar(sa.select(sa.func.count()).select_from(table)) != len(rows):
            raise RuntimeError("Retired storage restoration count does not match")
