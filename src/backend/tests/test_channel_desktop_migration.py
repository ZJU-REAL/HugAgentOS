"""Relay tables upgrade and rollback without touching a shared database."""

import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


def test_relay_migration_roundtrip(tmp_path):
    root = Path(__file__).resolve().parents[3]
    url = f"sqlite:///{tmp_path / 'relay-migration.sqlite'}"
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url)
    with engine.begin() as db:
        db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        db.execute(text("INSERT INTO alembic_version VALUES ('deskobs01')"))

    def migrate(*args):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    migrate("upgrade", "chanrelay01")
    inspector = inspect(engine)
    tables = {"desktop_channel_bindings", "channel_relay_deliveries", "channel_relay_operations"}
    assert tables.issubset(inspector.get_table_names())
    assert "ix_channel_relay_queue" in {
        row["name"] for row in inspector.get_indexes("channel_relay_deliveries")
    }
    migrate("downgrade", "deskobs01")
    assert not tables.intersection(inspect(engine).get_table_names())
    migrate("upgrade", "chanrelay01")
    assert tables.issubset(inspect(engine).get_table_names())
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("chanrelay01").down_revision == "deskobs01"
    engine.dispose()
