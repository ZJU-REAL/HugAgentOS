"""Chats: a server-side read position per user and chat.

Read state lived only in each browser, so a chat read on the web stayed unread in the
desktop app, on another computer or after a cleared cache. `chat_read_states.read_seq`
is the chat sequence the user has read up to; it only moves forward.

Existing chats are seeded as read up to their latest message, so the upgrade does not
light up every old chat. Chats created later have no row and start unread.

Same schema as the EE revision ``chatread01``.

Revision ID: ce_0044
Revises: ce_0043
"""
from alembic import op
import sqlalchemy as sa

from core.db.utc_datetime import UTCDateTime

revision = "ce_0044"
down_revision = "ce_0043"
branch_labels = None
depends_on = None

TABLE, INDEX = "chat_read_states", "idx_chat_read_states_chat"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        op.create_table(
            TABLE,
            sa.Column("user_id", sa.String(64), sa.ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("chat_id", sa.String(64), sa.ForeignKey("chat_sessions.chat_id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("read_seq", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("read_message_id", sa.String(64), nullable=True),
            sa.Column("read_at", UTCDateTime(timezone=True), nullable=True),
            sa.CheckConstraint("read_seq >= 0", name="chat_read_states_seq_nonnegative"),
        )
    if INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, ["chat_id"])
    # One statement; chats that already have a row keep it, so re-running is harmless.
    op.execute(sa.text(
        "INSERT INTO chat_read_states (user_id, chat_id, read_seq, read_at) "
        "SELECT s.user_id, s.chat_id, "
        "COALESCE((SELECT MAX(m.chat_seq) FROM chat_messages m WHERE m.chat_id = s.chat_id), 0), "
        "CURRENT_TIMESTAMP FROM chat_sessions s "
        "WHERE s.deleted_at IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM chat_read_states r WHERE r.user_id = s.user_id AND r.chat_id = s.chat_id)"
    ))


def downgrade():
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
