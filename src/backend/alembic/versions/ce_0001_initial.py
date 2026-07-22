"""社区版初始迁移：建出 CE 全部基线表（不建 EE 专属表）。

CE 走独立迁移链（不复用商业版历史迁移）。基线直接使用派生树中
``core.db.models`` 的 CE-only SQLAlchemy 元数据执行 ``create_all``——
方言感知（SQLite/PostgreSQL 通吃），与 ``api.app`` 启动兜底
``init_db`` 的 CE 分支同源，不会冲突（两者都幂等）。

共享个人资源表中仍需的跨版本 scope 列保留为 nullable，但团队表、
外键和团队专属字段不会注册到 CE 元数据。

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
