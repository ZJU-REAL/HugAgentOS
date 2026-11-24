"""Three-way task evaluation on message feedback: verdict (adopt / revise / reject) and reasons.

Additive only: ``rating`` keeps its like/dislike check so an older release can still write.
Existing rows get the verdict their rating implies. Indexes serve the evaluation reads: the
reminder (a user's recent runs), and the task monitor (evaluations given in a window, by answer).

Revision ID: ce_0048
Revises: ce_0047
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ce_0048"
down_revision = "ce_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message_feedback") as batch_op:
        batch_op.add_column(sa.Column("verdict", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("reasons", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=True))
    op.execute("UPDATE message_feedback SET verdict = 'adopt' WHERE rating = 'like' AND verdict IS NULL")
    op.execute("UPDATE message_feedback SET verdict = 'reject' WHERE rating = 'dislike' AND verdict IS NULL")
    op.execute("UPDATE message_feedback SET updated_at = created_at WHERE updated_at IS NULL")
    op.create_index("idx_message_feedback_updated_at", "message_feedback", ["updated_at"])
    op.create_index("idx_chat_runs_message_id", "chat_runs", ["message_id"])
    op.create_index("idx_chat_runs_user_updated", "chat_runs", ["user_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("idx_chat_runs_user_updated", table_name="chat_runs")
    op.drop_index("idx_chat_runs_message_id", table_name="chat_runs")
    op.drop_index("idx_message_feedback_updated_at", table_name="message_feedback")
    with op.batch_alter_table("message_feedback") as batch_op:
        batch_op.drop_column("reasons")
        batch_op.drop_column("verdict")
