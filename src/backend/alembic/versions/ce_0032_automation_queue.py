"""Durable queued automation invocations."""
from alembic import op

revision = "ce_0032"
down_revision = "ce_0031"
branch_labels = None
depends_on = None


def upgrade():
    from features.automation.schema import upgrade_queue
    upgrade_queue(op.get_bind())


def downgrade():
    # Refuse to erase queued work while downgrading the status contract.
    from sqlalchemy import text
    if op.get_bind().execute(text(
        "SELECT 1 FROM scheduled_task_runs WHERE status = 'pending' LIMIT 1"
    )).first():
        raise RuntimeError("Drain pending automation runs before downgrade")
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.drop_constraint("scheduled_task_runs_status_check", type_="check")
        batch.create_check_constraint("scheduled_task_runs_status_check",
                                      "status IN ('running', 'success', 'failed')")
