"""Preserve historical personal files before local SQLite unique indexes are added."""
from pathlib import PurePosixPath

import sqlalchemy as sa

from core.db.personal_file_names import INDEX_NAMES

JOURNAL = 'personal_filename_repairs'


def _create_indexes(connection, artifacts):
    existing = {row['name'] for row in sa.inspect(connection).get_indexes('artifacts')}
    scope = ''
    for name, columns, predicate in (
        (INDEX_NAMES[0], ('user_id', 'user_folder_id', 'filename'),
         'user_folder_id IS NOT NULL AND deleted_at IS NULL'),
        (INDEX_NAMES[1], ('user_id', 'filename'),
         'user_folder_id IS NULL AND deleted_at IS NULL'),
    ):
        if name not in existing:
            sa.Index(name, *(artifacts.c[key] for key in columns), unique=True,
                     sqlite_where=sa.text(scope + predicate)).create(connection)


def _replacement(filename, artifact_id, scope, occupied):
    suffix = PurePosixPath(filename).suffix
    stem = filename[:-len(suffix)] if suffix else filename
    attempt = 0
    while True:
        tag = '__' + artifact_id + (f'_{attempt}' if attempt else '') + suffix
        prefix = stem.encode('utf8')[:max(0, 255 - len(tag.encode('utf8')))]
        name = prefix.decode('utf8', errors='ignore') + tag
        if (*scope, name) not in occupied:
            return name
        attempt += 1


def reconcile_local_personal_filenames(bind):
    """Rename duplicate metadata atomically; never move bytes or change artifact IDs.

    The oldest row keeps its name. A durable journal records every change; the
    desktop installer's pre-upgrade database backup restores original names if
    startup rolls back. SQLite's write lock spans repairs and index creation.
    """
    report = {'renamed': 0}
    if bind.dialect.name != 'sqlite':
        return report
    with bind.begin() as connection:
        # Explicit BEGIN also makes SQLite DDL transactional with pysqlite.
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        inspector = sa.inspect(connection)
        tables = set(inspector.get_table_names())
        if 'artifacts' not in tables:
            return report
        columns = {item['name'] for item in inspector.get_columns('artifacts')}
        if not {'artifact_id', 'user_id', 'filename', 'title'}.issubset(columns):
            raise RuntimeError('Legacy artifacts schema lacks file identity columns')
        for name, kind in [('user_folder_id', 'VARCHAR(64)'), ('deleted_at', 'DATETIME')]:
            if name not in columns:
                connection.exec_driver_sql(f'ALTER TABLE artifacts ADD COLUMN {name} {kind}')
        metadata = sa.MetaData()
        artifacts = sa.Table('artifacts', metadata, autoload_with=connection)
        query = sa.select(artifacts).where(
            artifacts.c.deleted_at.is_(None), artifacts.c.user_id.is_not(None),
            artifacts.c.filename.is_not(None))
        ordering = ([artifacts.c.created_at] if 'created_at' in artifacts.c else [])
        rows = connection.execute(query.order_by(*ordering, artifacts.c.artifact_id)).mappings().all()
        occupied = {(r['user_id'], r['user_folder_id'], r['filename']) for r in rows}
        if 'user_folders' in tables:
            folders = sa.Table('user_folders', metadata, autoload_with=connection)
            query = sa.select(folders)
            if 'deleted_at' in folders.c:
                query = query.where(folders.c.deleted_at.is_(None))
            occupied.update((r['user_id'], r['parent_folder_id'], r['name'])
                            for r in connection.execute(query).mappings())
        seen = set()
        journal = None
        for row in rows:
            scope = (row['user_id'], row['user_folder_id'])
            key = (*scope, row['filename'])
            if key not in seen:
                seen.add(key)
                continue
            if journal is None:
                journal = sa.Table(JOURNAL, metadata,
                    sa.Column('artifact_id', sa.String(64), primary_key=True),
                    sa.Column('old_filename', sa.String(500), nullable=False),
                    sa.Column('old_title', sa.String(500)),
                    sa.Column('new_filename', sa.String(500), nullable=False),
                    sa.Column('new_title', sa.String(500)))
                journal.create(connection, checkfirst=True)
            name = _replacement(row['filename'], row['artifact_id'], scope, occupied)
            title = name if row['title'] == row['filename'] else row['title']
            connection.execute(journal.insert().values(
                artifact_id=row['artifact_id'], old_filename=row['filename'],
                old_title=row['title'], new_filename=name, new_title=title))
            connection.execute(artifacts.update().where(
                artifacts.c.artifact_id == row['artifact_id']).values(filename=name, title=title))
            occupied.add((*scope, name))
            report['renamed'] += 1
        _create_indexes(connection, artifacts)
    return report
