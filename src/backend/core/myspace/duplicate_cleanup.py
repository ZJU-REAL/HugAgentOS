"""Explicit, journalled removal of all live records in duplicate personal paths."""
from datetime import datetime, timezone
from typing import Callable
import sqlalchemy as sa


def _table(connection):
    return sa.Table("artifacts", sa.MetaData(), autoload_with=connection)


def duplicate_rows(connection):
    """Read-only preview. A duplicate group has no privileged 'winner'."""
    table = _table(connection)
    filters = [table.c.deleted_at.is_(None)]
    groups = (
        sa.select(table.c.user_id, table.c.user_folder_id, table.c.filename)
        .where(*filters)
        .group_by(table.c.user_id, table.c.user_folder_id, table.c.filename)
        .having(sa.func.count() > 1).subquery()
    )
    return [dict(row) for row in connection.execute(
        sa.select(table).join(groups, sa.and_(
            table.c.user_id == groups.c.user_id,
            table.c.user_folder_id.is_not_distinct_from(groups.c.user_folder_id),
            table.c.filename == groups.c.filename,
        )).where(*filters).order_by(table.c.artifact_id)
    ).mappings()]


def clean_duplicates(connection, *, before_delete: Callable | None = None):
    """Caller owns the transaction and stops writers until mirror cleanup completes.

    Tombstones retain file identities so message backfill cannot resurrect them.
    Original storage objects are retained for recovery, never reused as live paths.
    """
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE artifacts IN SHARE ROW EXCLUSIVE MODE"))
    rows = duplicate_rows(connection)
    timestamp = datetime.now(timezone.utc)
    journal = {"deleted_at": timestamp.isoformat(), "rows": rows}
    if before_delete is not None:
        before_delete(journal)  # Backup failure aborts before the first deletion.
    table = _table(connection)
    for offset in range(0, len(rows), 500):
        connection.execute(table.update().where(
            table.c.artifact_id.in_([r["artifact_id"] for r in rows[offset:offset+500]])
        ).values(deleted_at=timestamp, updated_at=timestamp))
    return journal


def restore_cleanup(connection, journal):
    """Restore unmodified tombstones; caller must downgrade the unique indexes first."""
    table = _table(connection)
    marker = datetime.fromisoformat(journal["deleted_at"])
    for row in journal["rows"]:
        old_updated = row.get("updated_at")
        if isinstance(old_updated, str):
            old_updated = datetime.fromisoformat(old_updated)
        connection.execute(table.update().where(
            table.c.artifact_id == row["artifact_id"],
            table.c.deleted_at == marker,
            table.c.updated_at == marker,
        ).values(deleted_at=None, updated_at=old_updated))
