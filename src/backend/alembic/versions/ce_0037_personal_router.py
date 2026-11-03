"""Personal Agent Router (same schema as the EE revision ``personalrouter01``): Triggers carry kind, lane and turn; leases move to personal_front_turns.

Stop the turn workers before upgrading: claimed rows go back to pending and the new
scheduler claims them again. A downgrade restores the columns, not the leases.
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "ce_0037"
down_revision = "ce_0036"
branch_labels = None
depends_on = None

INBOX = "personal_message_inbox"
TURNS = "personal_front_turns"


def _kind(payload):
    if payload.get("kind") == "heartbeat": return "heartbeat"
    if payload.get("kind") == "bootstrap": return "bootstrap"
    if payload.get("kind") == "task_event":
        return "session_run" if payload.get("event") == "session_run_finished" else "task_result"
    return "message"


def _json(value):
    if isinstance(value, str):
        try: return json.loads(value) or {}
        except ValueError: return {}
    return dict(value or {})


def upgrade():
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if TURNS not in tables:
        op.create_table(
            TURNS,
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("agent_id", sa.String(64), sa.ForeignKey("personal_agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("chat_id", sa.String(64), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="running"),
            sa.Column("owner", sa.String(100)),
            sa.Column("lease_until", sa.Float()),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("reply_ids", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_pa_front_turn_agent", TURNS, ["agent_id", "status"])
    if INBOX not in tables: return
    columns = {c["name"] for c in sa.inspect(bind).get_columns(INBOX)}
    with op.batch_alter_table(INBOX) as batch:
        if "kind" not in columns: batch.add_column(sa.Column("kind", sa.String(24), nullable=False, server_default="message"))
        if "chat_id" not in columns: batch.add_column(sa.Column("chat_id", sa.String(64)))
        if "turn_id" not in columns: batch.add_column(sa.Column("turn_id", sa.String(64)))
    op.execute(sa.text("UPDATE personal_message_inbox SET chat_id = (SELECT chat_id FROM chat_messages"
                       " WHERE chat_messages.message_id = personal_message_inbox.message_id) WHERE chat_id IS NULL"))
    # A turn that was running belongs to no Turn yet: the new scheduler claims it again.
    op.execute(sa.text("UPDATE personal_message_inbox SET status = 'pending' WHERE status IN ('responding', 'routing', 'claimed')"))
    rows = bind.execute(sa.text("SELECT message_id, payload, decision FROM personal_message_inbox")).all()
    for message_id, payload, decision in rows:
        kind, decision = _kind(_json(payload)), _json(decision)
        changes = {}
        if kind != "message": changes["kind"] = kind
        if "attempts" in decision:
            decision.pop("attempts")
            changes["decision"] = decision
        if changes:
            sets = ", ".join(name + " = :" + name for name in changes)
            statement = sa.text("UPDATE personal_message_inbox SET " + sets + " WHERE message_id = :message_id")
            if "decision" in changes: statement = statement.bindparams(sa.bindparam("decision", type_=sa.JSON()))
            bind.execute(statement, {**changes, "message_id": message_id})
    with op.batch_alter_table(INBOX) as batch:
        for column in ("owner", "lease_until"):
            if column in columns: batch.drop_column(column)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(INBOX)}
    if "ix_pa_inbox_turn" not in indexes: op.create_index("ix_pa_inbox_turn", INBOX, ["turn_id"])


def downgrade():
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if INBOX in tables:
        op.execute(sa.text("UPDATE personal_message_inbox SET status = 'pending' WHERE status = 'claimed'"))
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(INBOX)}
        if "ix_pa_inbox_turn" in indexes: op.drop_index("ix_pa_inbox_turn", table_name=INBOX)
        columns = {c["name"] for c in sa.inspect(bind).get_columns(INBOX)}
        with op.batch_alter_table(INBOX) as batch:
            if "owner" not in columns: batch.add_column(sa.Column("owner", sa.String(100)))
            if "lease_until" not in columns: batch.add_column(sa.Column("lease_until", sa.Float()))
            for column in ("kind", "chat_id", "turn_id"):
                if column in columns: batch.drop_column(column)
    if TURNS in tables:
        op.drop_index("ix_pa_front_turn_agent", table_name=TURNS)
        op.drop_table(TURNS)
