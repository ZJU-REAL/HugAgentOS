"""Legacy DBs with kind='personal'-only CHECK self-heal to allow local projects."""

from __future__ import annotations

from sqlalchemy import create_engine

from core.db.schema_reconcile import _patch_projects_kind_constraint_sqlite


def _make_legacy_projects(conn):
    conn.exec_driver_sql(
        "CREATE TABLE projects ("
        " project_id VARCHAR(64) PRIMARY KEY, name VARCHAR(120) NOT NULL,"
        " kind VARCHAR(16) NOT NULL, pinned BOOLEAN NOT NULL,"
        " CONSTRAINT ck_projects_kind_personal CHECK (kind = 'personal'))"
    )


def test_legacy_constraint_is_relaxed_to_allow_local(tmp_path):
    # File DB so a fresh connection re-reads the amended schema, matching prod
    # (per-request connections), unlike a per-connection in-memory DB.
    engine = create_engine(f"sqlite:///{tmp_path / 'proj.db'}")
    with engine.begin() as conn:
        _make_legacy_projects(conn)
        # before: local insert rejected
        try:
            conn.exec_driver_sql(
                "INSERT INTO projects VALUES ('p1','x','local',0)"
            )
            raise AssertionError("legacy constraint should reject 'local'")
        except Exception:
            pass
        patched = _patch_projects_kind_constraint_sqlite(conn)
        assert patched is True
    # reconnect so the amended schema takes effect, then local insert works
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO projects VALUES ('p2','y','local',0)")
        conn.exec_driver_sql("INSERT INTO projects VALUES ('p3','z','personal',0)")
        n = conn.exec_driver_sql("SELECT COUNT(*) FROM projects").fetchone()[0]
        assert n == 2


def test_patch_is_idempotent_and_noop_when_absent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'proj2.db'}")
    with engine.begin() as conn:
        # no projects table → no-op
        assert _patch_projects_kind_constraint_sqlite(conn) is False
        _make_legacy_projects(conn)
        assert _patch_projects_kind_constraint_sqlite(conn) is True
    with engine.begin() as conn:
        # already patched → no-op second time
        assert _patch_projects_kind_constraint_sqlite(conn) is False
