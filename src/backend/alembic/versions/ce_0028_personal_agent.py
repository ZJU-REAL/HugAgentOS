"""Add shared Personal Agent identities, tasks, devices and durable inboxes."""
from alembic import op
import sqlalchemy as sa

revision = "ce_0028"
down_revision = "ce_0027"
branch_labels = None
depends_on = None

TABLES = (
    "personal_agents", "personal_work_items", "personal_devices",
    "personal_device_commands", "personal_message_tasks", "personal_message_inbox",
    "personal_session_operations",
)


def upgrade():
    # CE bootstrap also creates current metadata; checkfirst handles fresh installs.
    from core.db.personal_schema import ce_metadata
    metadata = ce_metadata()
    metadata.create_all(op.get_bind(), tables=[metadata.tables[name] for name in TABLES], checkfirst=True)


def downgrade():
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    for name in TABLES:
        if name in present and bind.execute(sa.text('SELECT 1 FROM "' + name + '" LIMIT 1')).first():
            raise RuntimeError("Personal Agent data exists; export and remove it explicitly before downgrade")
    for name in reversed(TABLES):
        if name in present:
            op.drop_table(name)
