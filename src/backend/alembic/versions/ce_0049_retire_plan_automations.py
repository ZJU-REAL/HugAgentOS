"""Retire scheduled plan execution while preserving task history."""

import sqlalchemy as sa
from alembic import op

revision = "ce_0049"
down_revision = "ce_0048"
branch_labels = None
depends_on = None

RETIRED_MESSAGE = "执行计划类型的定时任务已下线，请改用提示词重新创建任务"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE scheduled_tasks SET status = 'disabled', next_run_at = NULL, "
            "manual_trigger_at = NULL, last_error = :message WHERE task_type = 'plan'"
        ).bindparams(message=RETIRED_MESSAGE)
    )


def downgrade() -> None:
    # Disabled tasks remain disabled so a downgrade cannot restart them.
    pass
