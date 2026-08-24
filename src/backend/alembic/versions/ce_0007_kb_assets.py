"""CE: add kb_assets table (knowledge-base media assets)

Revision ID: ce_0007
Revises: ce_0006
Create Date: 2026-08-18

知识库的非文本资产表（版面解析切出的图片；后续音视频同表）。主仓的
``kbasset01`` 不会进 CE —— manifest 把主仓 alembic 链整体排除，CE 走自己这条。

⚠️ 建表**必须条件化**（同 ce_0005 / ce_0006）：``ce_0001`` 用 ``ce_create_all`` 直接照
SQLAlchemy 模型建库，而 ``KBAsset`` 已经进了 ``core.db.models`` 的导出，所以**全新安装**
走到这一步时表早就存在了——无条件 ``create_table`` 会让每一次全新部署的
``alembic upgrade head`` 死在 "table kb_assets already exists"。只有在该模型之前建出来的
老 CE 库才真的需要这条迁移建表。

介质无关的设计（``kind`` 放宽到 image/audio/video、``locator`` 存位置、``vector_state``
记录已进入哪些向量空间）见主仓迁移 ``kbasset01`` 的说明，两边字段保持一致。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ce_0007"
down_revision = "ce_0006"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    if "kb_assets" in _tables(op.get_bind()):
        return

    op.create_table(
        "kb_assets",
        sa.Column("asset_id", sa.String(64), primary_key=True),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("chunk_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="image"),
        sa.Column(
            "mime_type", sa.String(100), nullable=False, server_default="application/octet-stream"
        ),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0"),
        sa.Column("asset_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locator", JSONB),
        sa.Column("caption", sa.Text()),
        sa.Column("text_content", sa.Text()),
        sa.Column("caption_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("vector_state", JSONB),
        sa.Column("metadata", JSONB),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["kb_id"], ["kb_spaces.kb_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["kb_documents.document_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("kind IN ('image', 'audio', 'video')", name="kb_assets_kind_check"),
    )
    op.create_index("idx_kb_assets_kb_id", "kb_assets", ["kb_id"])
    op.create_index("idx_kb_assets_document_id", "kb_assets", ["document_id"])
    op.create_index("idx_kb_assets_chunk_id", "kb_assets", ["chunk_id"])


def downgrade() -> None:
    op.drop_index("idx_kb_assets_chunk_id", table_name="kb_assets")
    op.drop_index("idx_kb_assets_document_id", table_name="kb_assets")
    op.drop_index("idx_kb_assets_kb_id", table_name="kb_assets")
    op.drop_table("kb_assets")
