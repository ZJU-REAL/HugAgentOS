"""Add durable command tasks to existing community databases."""
from alembic import op
from core.db.models.command import CommandTask

revision = "ce_0030"
down_revision = "ce_0029"
branch_labels = None
depends_on = None

def upgrade():
    CommandTask.__table__.create(bind=op.get_bind(), checkfirst=True)

def downgrade():
    CommandTask.__table__.drop(bind=op.get_bind(), checkfirst=True)
