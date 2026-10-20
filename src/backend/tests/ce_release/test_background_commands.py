"""Community command routes, metadata and repeatable migration."""
import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

def test_command_routes_and_existing_database_upgrade():
    from api.routes.v1 import CE_ROUTERS
    from core.db.models import CommandTask
    assert ("commands", "router") in CE_ROUTERS
    script = Path(__file__).parents[2] / "alembic/versions/ce_0030_background_commands.py"
    spec = importlib.util.spec_from_file_location("command_migration", script)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            columns = {c["name"] for c in inspect(connection).get_columns("command_tasks")}
            assert columns == {c.name for c in CommandTask.__table__.columns}
            migration.downgrade()
            assert "command_tasks" not in inspect(connection).get_table_names()
    engine.dispose()
