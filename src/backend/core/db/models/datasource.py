"""SQLAlchemy ORM model — data sources (unified "database tools" configuration).

One DataSource = one database connection managed by the "database tools" feature.
ds_type decides which MCP tool is exposed to the agent:

  - ``external_nl2sql`` → exposes the ``query_database`` tool (external NL2SQL HTTP service).
  - other SQL types (mysql/postgresql/sqlserver/mariadb/sqlite) → expose the
    ``execute_sql`` / ``search_objects`` tools via the DBHub sidecar.

DSN/passwords are stored in plaintext (consistent with the repo's existing
system_configs / admin_mcp_servers); API responses are masked.
"""

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String,
    TIMESTAMP, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from core.db.engine import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")

# Supported data source types:
#   external_nl2sql → the legacy query_database tool; elasticsearch → the es_query tool; other SQL → db_query (DBHub).
DS_TYPES = (
    "external_nl2sql", "mysql", "postgresql", "sqlserver", "mariadb", "sqlite", "elasticsearch",
)


class DataSource(Base):
    """Data source configuration row for the "database tools" feature.

    Connection params prefer the structured fields (host/port/username/password/database),
    from which the backend builds the DSN / ES_URL; ``dsn`` is only an advanced
    "raw connection string override". external_nl2sql uses ``url``.
    """

    __tablename__ = "data_sources"

    id          = Column(String(64), primary_key=True)   # slug, also used as the dbhub source id
    name        = Column(String(255), nullable=False)
    ds_type     = Column(String(32), nullable=False, default="mysql")
    # Structured connection params (stored plaintext; API masks password)
    host        = Column(String(255))
    port        = Column(Integer)
    username    = Column(String(255))
    password    = Column(Text)        # plaintext, masked in the API
    database    = Column(String(255)) # database name / sqlite file path / ES index pattern
    dsn         = Column(Text)        # advanced: raw DSN override (takes precedence when filled)
    url         = Column(Text)        # external_nl2sql service address
    description = Column(Text)
    readonly    = Column(Boolean, nullable=False, default=True)
    is_enabled  = Column(Boolean, nullable=False, default=True)
    sort_order  = Column(Integer, nullable=False, default=0)
    extra       = Column(JSONType, default=dict)   # search_path / api_key / ssl_skip_verify / custom keys, etc.
    created_at  = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at  = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "ds_type IN ('external_nl2sql','mysql','postgresql','sqlserver','mariadb','sqlite','elasticsearch')",
            name="data_sources_ds_type_check",
        ),
        Index("idx_data_sources_enabled", "is_enabled"),
        Index("idx_data_sources_sort", "sort_order"),
    )


# ── Metadata governance (improves direct-connection retrieval accuracy) ────────
#
# A direct-connection data source (non external_nl2sql) can carry a set of metadata:
# table/column business semantics + enum dictionaries + golden Q→SQL exemplars. At
# retrieval time the built-in tool ``get_data_context`` recalls them on demand and
# feeds them to the model (**not into the system prompt**). The external NL2SQL black
# box (external_nl2sql) does text2sql internally and cannot be fed this metadata, so
# it is not annotated in this domain. See
# internal design docs.


class DsTableMeta(Base):
    """Table-level metadata: business name, description, synonyms, whitelist / deprecation flags."""

    __tablename__ = "ds_table_meta"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    datasource_id = Column(String(64), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False)
    schema_name   = Column(String(128), nullable=False, default="")  # database/schema (PG multi-schema / ES)
    table_name    = Column(String(255), nullable=False)
    display_name  = Column(String(255))            # business name, e.g. "订单主表" (order master table)
    description   = Column(Text)                    # business-definition notes
    synonyms      = Column(JSONType, default=list)  # synonyms/aliases (matching user phrasing)
    lifecycle     = Column(String(16))             # None / 'certified' / 'deprecated' (a "don't use" signal)
    is_whitelisted = Column(Boolean, nullable=False, default=True)  # whether it enters the data dictionary
    row_estimate  = Column(Integer)                 # probed magnitude (aids judgment)
    sort_order    = Column(Integer, nullable=False, default=0)
    created_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("datasource_id", "schema_name", "table_name", name="uq_ds_table_meta"),
        Index("idx_ds_table_meta_ds", "datasource_id"),
    )


class DsColumnMeta(Base):
    """Column-level metadata: business name, description, enum dictionary (status=1→approved), semantic role, foreign keys, etc.

    The two highest-ROI fields are ``value_map`` (the enum dictionary, directly attacking
    "values are codes with no dictionary") and golden SQL (separate table). ``foreign_key``
    holds ``other_table.other_col``, supplementing relationship declarations missing in the
    external database so the model doesn't write JOINs wrong or omit them.
    """

    __tablename__ = "ds_column_meta"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    datasource_id = Column(String(64), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False)
    schema_name   = Column(String(128), nullable=False, default="")
    table_name    = Column(String(255), nullable=False)
    column_name   = Column(String(255), nullable=False)
    display_name  = Column(String(255))            # business name, e.g. "订单状态" (order status)
    description   = Column(Text)
    synonyms      = Column(JSONType, default=list)
    data_type     = Column(String(64))             # probed physical type
    semantic_role = Column(String(16))             # 'dimension' / 'measure' / 'time'
    value_map     = Column(JSONType, default=dict)  # enum dictionary, e.g. {"1":"已审核","2":"已驳回"}
    sample_values = Column(JSONType, default=list)  # sample values (backfilled from probe sampling)
    unit_format   = Column(JSONType, default=dict)  # {type: currency, currency_code: CNY}, etc.
    is_pii        = Column(Boolean, nullable=False, default=False)
    lifecycle     = Column(String(16))             # None / 'certified' / 'deprecated'
    is_primary_key = Column(Boolean, nullable=False, default=False)
    foreign_key   = Column(String(512))            # 'other_table.other_col'
    sort_order    = Column(Integer, nullable=False, default=0)
    created_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("datasource_id", "schema_name", "table_name", "column_name",
                         name="uq_ds_column_meta"),
        Index("idx_ds_column_meta_ds", "datasource_id"),
        Index("idx_ds_column_meta_tbl", "datasource_id", "schema_name", "table_name"),
    )


class DsGoldenSql(Base):
    """Golden Q→SQL pairs: human-verified correct "question → SQL" exemplars (the #1 accuracy lever).

    At retrieval time they are recalled by similarity (embedding from Phase 2; in
    Phase 1 the small corpus is simply loaded in full) and injected into the model as
    few-shot examples, turning business-side tribal knowledge into a governed asset.
    """

    __tablename__ = "ds_golden_sql"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    datasource_id = Column(String(64), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False)
    question      = Column(Text, nullable=False)
    sql           = Column(Text, nullable=False)
    tables_used   = Column(JSONType, default=list)  # tables involved (eases per-table trimming/aggregation)
    status        = Column(String(16), nullable=False, default="candidate")  # 'verified' / 'candidate'
    hit_count     = Column(Integer, nullable=False, default=0)
    verified_by   = Column(String(64))
    verified_at   = Column(TIMESTAMP(timezone=True))
    sort_order    = Column(Integer, nullable=False, default=0)
    created_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at    = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_ds_golden_sql_ds", "datasource_id"),
    )
