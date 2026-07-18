"""社区版初始迁移：建出 CE 全部基线表（不建 EE 专属表）。

CE 走独立迁移链（不复用商业版的 52 个迁移）。基线以
``core.db.models`` 的 SQLAlchemy 元数据为源、按
``core.db.edition_tables.EE_ONLY_TABLES`` 过滤后 ``create_all``——
方言感知（SQLite/PostgreSQL 通吃），与 ``api.app`` 启动兜底
``init_db`` 的 CE 分支同源同滤，不会冲突（两者都幂等）。

跨边界 FK 列（``team_id``/``team_folder_id``/``share_scope`` 等）按
方案 D3 保留为 nullable，CE 恒 NULL——降低与主仓 models 包的分叉。

后续 CE schema 演进在本链上追加常规 alembic 迁移。

Revision ID: ce_0001
Revises:
Create Date: 2026-06-10
"""

from alembic import op

revision = "ce_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from core.db.edition_tables import ce_create_all

    ce_create_all(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("CE 基线迁移不支持降级")
