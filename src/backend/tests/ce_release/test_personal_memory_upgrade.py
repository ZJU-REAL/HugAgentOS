"""Exercise memory upgrades and proactive ownership filters in the derived CE tree."""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from core.db.edition import ce_create_all
from core.db.models import ChatSession, UserShadow


def test_memory_upgrade_keeps_existing_records(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'memory.db'}")
    path = Path(__file__).resolve().parents[2] / "alembic/versions/ce_0042_personal_memory.py"
    spec = importlib.util.spec_from_file_location("ce_memory_upgrade", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE personal_agents (id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO personal_agents VALUES ('agent')")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        connection.exec_driver_sql(
            "INSERT INTO personal_memory_versions "
            "(id, agent_id, path, writer, before_text, after_text, created_at) "
            "VALUES ('version', 'agent', 'memory/topic-test.md', 'user', 'old', 'new', CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO personal_memory_usage (agent_id, path, first_seen_at) "
            "VALUES ('agent', 'memory/topic-test.md', CURRENT_TIMESTAMP)"
        )
        migration.upgrade()
        assert connection.exec_driver_sql(
            "SELECT before_text, after_text FROM personal_memory_versions"
        ).one() == ("old", "new")
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM personal_memory_usage").scalar_one() == 1
        assert connection.exec_driver_sql("SELECT id FROM personal_agents").scalar_one() == "agent"
    engine.dispose()


def test_proactive_probe_only_reads_owner_web_chats(tmp_path):
    from personal_agent.scheduling.proactive_probe import _reviewable

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'probe.db'}")
    ce_create_all(engine)
    with engine.begin() as connection:
        for user in ("owner", "other"):
            connection.execute(UserShadow.__table__.insert().values(
                user_id=user, user_center_id=user, username=user))
        for chat, user, extra in (
            ("own", "owner", {}),
            ("other", "other", {}),
            ("automation", "owner", {"automation_run": True}),
        ):
            connection.execute(ChatSession.__table__.insert().values(
                chat_id=chat, user_id=user, title=chat, metadata=extra))
        connection.execute(ChatSession.__table__.insert().values(
            chat_id="channel", user_id="owner", title="channel", channel_id="bot"))
        rows = connection.execute(sa.select(ChatSession.chat_id).where(_reviewable("owner"))).scalars().all()
        assert rows == ["own"]
    engine.dispose()
