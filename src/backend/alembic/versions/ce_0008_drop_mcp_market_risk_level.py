"""CE: replace guessed MCP risk fields with a neutral listing notice.

Revision ID: ce_0008
Revises: ce_0007
Create Date: 2026-09-02

CE owns an independent Alembic chain, so the corresponding FULL migration is
not copied into the derived repository. Older CE databases contain the
risk_level and risk_report columns; fresh databases created from current
metadata already contain only listing_notice. This migration handles both
paths, including a hybrid schema produced if runtime reconciliation added the
new nullable column before Alembic ran.
"""

import sqlalchemy as sa
from alembic import op

revision = "ce_0008"
down_revision = "ce_0007"
branch_labels = None
depends_on = None

_TABLES = (
    ("mcp_market_versions", "mcp_market_versions_risk_check"),
    ("mcp_market_submissions", "mcp_market_submissions_risk_check"),
)


def _column_names(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _check_names(bind, table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints(table)
        if constraint.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    existing_tables = set(sa.inspect(bind).get_table_names())

    for table, constraint in _TABLES:
        if table not in existing_tables:
            continue
        columns = _column_names(bind, table)
        has_risk_level = "risk_level" in columns
        has_risk_report = "risk_report" in columns
        has_listing_notice = "listing_notice" in columns
        if not has_risk_level and not has_risk_report:
            continue

        if has_risk_report and has_listing_notice:
            bind.execute(
                sa.text(
                    f'UPDATE "{table}" SET listing_notice = risk_report '
                    "WHERE listing_notice IS NULL"
                )
            )

        if not is_sqlite and constraint in _check_names(bind, table):
            op.drop_constraint(constraint, table, type_="check")

        with op.batch_alter_table(table) as batch:
            if has_risk_level:
                batch.drop_column("risk_level")
            if has_risk_report:
                if has_listing_notice:
                    batch.drop_column("risk_report")
                else:
                    batch.alter_column("risk_report", new_column_name="listing_notice")


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    existing_tables = set(sa.inspect(bind).get_table_names())

    for table, constraint in _TABLES:
        if table not in existing_tables:
            continue
        columns = _column_names(bind, table)
        with op.batch_alter_table(table) as batch:
            if "listing_notice" in columns and "risk_report" not in columns:
                batch.alter_column("listing_notice", new_column_name="risk_report")
            if "risk_level" not in columns:
                batch.add_column(
                    sa.Column(
                        "risk_level",
                        sa.String(length=16),
                        nullable=False,
                        server_default="low",
                    )
                )

        if not is_sqlite and constraint not in _check_names(bind, table):
            op.create_check_constraint(
                constraint,
                table,
                "risk_level IN ('low', 'medium', 'high')",
            )
