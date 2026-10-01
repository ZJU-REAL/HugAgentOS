"""Historical personal files survive the desktop uniqueness upgrade."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from core.db.local_personal_file_upgrade import reconcile_local_personal_filenames


def legacy_database(tmp_path):
    engine = sa.create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    metadata = sa.MetaData()
    table = sa.Table('artifacts', metadata,
        sa.Column('artifact_id', sa.String, primary_key=True),
        sa.Column('user_id', sa.String), sa.Column('user_folder_id', sa.String),
        sa.Column('team_id', sa.String), sa.Column('filename', sa.String),
        sa.Column('title', sa.String), sa.Column('storage_key', sa.String),
        sa.Column('created_at', sa.DateTime), sa.Column('deleted_at', sa.DateTime))
    folders = sa.Table('user_folders', metadata,
        sa.Column('folder_id', sa.String, primary_key=True),
        sa.Column('user_id', sa.String), sa.Column('parent_folder_id', sa.String),
        sa.Column('name', sa.String), sa.Column('deleted_at', sa.DateTime))
    metadata.create_all(engine)
    return engine, table, folders


def test_three_historical_groups_keep_bytes_ids_and_references(tmp_path):
    engine, table, folders = legacy_database(tmp_path)
    names = ['L4产品族颗粒度诊断.html', 'l4l5-graph-builder.tgz',
             '北极星高新产业技术预见平台周报_2026-09-05_to_2026-09-11.docx']
    rows = [dict(artifact_id=f'{group}-{item}', user_id='u', user_folder_id=None,
                 team_id=None, filename=name, title=name, storage_key=f'bytes/{group}-{item}',
                 created_at=datetime(2026, 1, item + 1))
            for group, name in enumerate(names) for item in range(2)]
    with engine.begin() as conn:
        conn.execute(table.insert(), rows)
    report = reconcile_local_personal_filenames(engine)
    assert report['renamed'] == 3
    with engine.connect() as conn:
        after = {r['artifact_id']:r for r in conn.execute(sa.select(table)).mappings()}
        for row in rows:
            current = after[row['artifact_id']]
            assert current['storage_key'] == row['storage_key']
            assert current['deleted_at'] is None
            if row['artifact_id'].endswith('-0'):
                assert current['filename'] == row['filename']
            else:
                assert current['filename'] != row['filename']
                assert current['filename'].endswith(row['filename'].split('.')[-1])
        assert conn.execute(sa.text('SELECT count(*) FROM personal_filename_repairs')).scalar() == 3
        assert len(after) == 6
    assert reconcile_local_personal_filenames(engine)['renamed'] == 0
    engine.dispose()


def test_scopes_existing_suffix_folders_deleted_rows_and_custom_titles(tmp_path):
    engine, table, folders = legacy_database(tmp_path)
    rows = [dict(artifact_id=ident, user_id=user, user_folder_id=folder,
                 team_id=team, filename=name, title='Custom', storage_key='bytes/'+ident,
                 deleted_at=datetime(2026, 1, 1) if deleted else None)
            for ident,user,folder,team,name,deleted in [
                ('a','u',None,None,'file.html',False),
                ('b','u',None,None,'file.html',False),
                ('occupied','u',None,None,'file__b.html',False),
                ('other','v',None,None,'file.html',False),
                ('folder','u','f',None,'file.html',False),
                ('deleted','u',None,None,'file.html',True)]]
    with engine.begin() as conn:
        conn.execute(table.insert(),rows)
        conn.execute(folders.insert(),dict(folder_id='reserved',user_id='u',name='file__b_1.html'))
    assert reconcile_local_personal_filenames(engine)['renamed'] == 1
    with engine.connect() as conn:
        after={r.artifact_id:r for r in conn.execute(sa.select(table))}
        assert after['b'].filename == 'file__b_2.html'
        assert after['b'].title == 'Custom'
        assert all(after[r['artifact_id']].filename == r['filename'] for r in rows if r['artifact_id']!='b')
        assert {'uq_personal_file_root_name','uq_personal_file_folder_name'} <= {i['name'] for i in sa.inspect(conn).get_indexes('artifacts')}
    engine.dispose()


def test_utf8_limit_and_transaction_rollback(tmp_path, monkeypatch):
    engine, table, _ = legacy_database(tmp_path)
    name='文'*81+'.html'
    with engine.begin() as conn:
        conn.execute(table.insert(),[dict(artifact_id=key,user_id='u',filename=name,title=name)
                                     for key in ['a','b']])
    from core.db import local_personal_file_upgrade as upgrade
    original=upgrade._create_indexes
    def fail(*args):
        raise RuntimeError('index creation failed')
    monkeypatch.setattr(upgrade,'_create_indexes',fail)
    with pytest.raises(RuntimeError,match='index creation failed'):
        reconcile_local_personal_filenames(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.select(table.c.filename)).scalars().all() == [name,name]
    monkeypatch.setattr(upgrade,'_create_indexes',original)
    reconcile_local_personal_filenames(engine)
    with engine.connect() as conn:
        repaired=conn.execute(sa.select(table.c.filename).where(table.c.artifact_id=='b')).scalar()
        assert len(repaired.encode('utf8')) <=255
        assert repaired.endswith('.html')
    engine.dispose()


def test_real_local_startup_repairs_before_schema_reconciliation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.db import engine as db_engine

    from core.db.models import Artifact
    engine = sa.create_engine('sqlite:///' + str(tmp_path / 'old-desktop.db'))
    db_engine.Base.metadata.create_all(engine)
    table = Artifact.__table__
    with engine.begin() as conn:
        for name in ['uq_personal_file_root_name','uq_personal_file_folder_name']:
            conn.exec_driver_sql('DROP INDEX ' + name)
        conn.execute(table.insert(), [dict(artifact_id=key, user_id='u', filename='file.html',
                     title='file.html', storage_key='bytes/' + key, type='other',
                     mime_type='text/html',size_bytes=1) for key in ['a','b']])
    monkeypatch.setattr(db_engine, 'engine', engine)
    monkeypatch.setattr(db_engine, 'settings', SimpleNamespace(
        edition=SimpleNamespace(edition='ce'), deploy=SimpleNamespace(is_local=True)))
    monkeypatch.setattr(db_engine, '_merge_duplicate_identities', lambda:None)
    db_engine.init_db()
    db_engine.init_db()
    with engine.connect() as conn:
        assert conn.execute(sa.select(table.c.filename).order_by(table.c.artifact_id)).scalars().all() == ['file.html','file__b.html']
    engine.dispose()
