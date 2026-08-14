"""SQLAlchemy ORM models — 自建知识库的 LLM Wiki 层。

Wiki 是知识库的**结构地图**：管线从原文分块里抽出实体页与概念页，为每页写一段带
出处的 Markdown，页与页之间用 ``[[slug|显示名]]`` 互链成图。它不是第二个检索源——
答案与出处始终回到 ``kb_chunks`` 的原文，页上的 ``source_refs`` / ``chunk_refs``
就是回原文的血缘坐标。

与外接知识库的 Wiki 透传不同，这三张表与 ``kb_documents`` / ``kb_chunks`` 同库，
血缘可以按 ID 直接 JOIN 取回，不需要「拉整篇文档再本地过滤」。
"""

from datetime import datetime

from core.db.engine import Base
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

JSONType = JSON().with_variant(JSONB(), "postgresql")

# 目录树最大层级。提示词只要 2 级，这里留 3 级是防御性上限：模型偶尔多给一层、
# 或后续 remap 叠了一层时，不至于长出无边界的面包屑。
WIKI_CATEGORY_MAX_DEPTH = 3

# 页面类型
WIKI_PAGE_TYPE_SUMMARY = "summary"  # 单篇源文档的摘要页，slug = summary/<document_id>
WIKI_PAGE_TYPE_ENTITY = "entity"  # 实体页（人/组织/产品/地点/技术/事件）
WIKI_PAGE_TYPE_CONCEPT = "concept"  # 概念页（主题/方法/理论）
WIKI_PAGE_TYPE_INDEX = "index"  # 全库索引页
WIKI_PAGE_TYPE_SYNTHESIS = "synthesis"  # 综述页——管线不产，仅由智能体写入
WIKI_PAGE_TYPE_COMPARISON = "comparison"  # 对比页——管线不产，仅由智能体写入

WIKI_PAGE_TYPES = (
    WIKI_PAGE_TYPE_SUMMARY,
    WIKI_PAGE_TYPE_ENTITY,
    WIKI_PAGE_TYPE_CONCEPT,
    WIKI_PAGE_TYPE_INDEX,
    WIKI_PAGE_TYPE_SYNTHESIS,
    WIKI_PAGE_TYPE_COMPARISON,
)

# 「知识」视图合并展示的页面类型：这四类形态相近，索引页与文档摘要页各自独立成组
WIKI_KNOWLEDGE_PAGE_TYPES = (
    WIKI_PAGE_TYPE_ENTITY,
    WIKI_PAGE_TYPE_CONCEPT,
    WIKI_PAGE_TYPE_SYNTHESIS,
    WIKI_PAGE_TYPE_COMPARISON,
)

# 页面类型 → 中文展示名
WIKI_PAGE_TYPE_LABELS = {
    WIKI_PAGE_TYPE_CONCEPT: "概念",
    WIKI_PAGE_TYPE_ENTITY: "实体",
    WIKI_PAGE_TYPE_SYNTHESIS: "综述",
    WIKI_PAGE_TYPE_COMPARISON: "对比",
    WIKI_PAGE_TYPE_SUMMARY: "文档摘要",
    WIKI_PAGE_TYPE_INDEX: "总索引",
}

# 编辑来源：区分「这段是管线写的还是人改的」
WIKI_EDIT_SOURCE_PIPELINE = "pipeline"
WIKI_EDIT_SOURCE_AGENT = "agent"
WIKI_EDIT_SOURCE_USER = "user"


class KBWikiFolder(Base):
    """Wiki 目录节点（邻接表）。

    ``parent_id`` 为空串表示根层。``path`` 是用 ``/`` 连接的名称链（物化路径），
    只为展示与筛选服务；归属关系的真源永远是 ``parent_id``。空目录允许独立存在，
    这样用户可以先搭骨架再往里填页。
    """

    __tablename__ = "kb_wiki_folders"

    folder_id = Column(String(64), primary_key=True)
    kb_id = Column(String(64), ForeignKey("kb_spaces.kb_id", ondelete="CASCADE"), nullable=False)
    parent_id = Column(String(64), nullable=False, default="")
    name = Column(String(255), nullable=False)
    path = Column(String(1024), nullable=False, default="")
    depth = Column(Integer, nullable=False, default=0)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("idx_kb_wiki_folders_kb_parent", "kb_id", "parent_id"),
        Index(
            "idx_kb_wiki_folders_unique_name",
            "kb_id",
            "parent_id",
            "name",
            unique=True,
            postgresql_where=Column("deleted_at").is_(None),
        ),
    )


class KBWikiPage(Base):
    """一个 Wiki 页面。

    ``folder_id`` 是页面归属目录的唯一真源；``category_path`` / ``wiki_path`` /
    ``depth`` / ``sort_order`` 都是从目录链派生出来的缓存投影，重建目录时会一并刷新。

    血缘两列的语义要分清：``source_refs`` 是该页取材自哪些文档
    （``kb_documents.document_id``），``chunk_refs`` 是具体哪几个父块支撑了页面内容
    （``kb_chunks.chunk_id``）。回溯原文按后者直取，前者用于文档删除时定位受影响页。
    """

    __tablename__ = "kb_wiki_pages"

    page_id = Column(String(64), primary_key=True)
    kb_id = Column(String(64), ForeignKey("kb_spaces.kb_id", ondelete="CASCADE"), nullable=False)
    slug = Column(String(255), nullable=False)
    title = Column(String(512), nullable=False, default="")
    page_type = Column(String(32), nullable=False, default=WIKI_PAGE_TYPE_SUMMARY)
    status = Column(String(32), nullable=False, default="published")
    content = Column(Text, nullable=False, default="")
    summary = Column(Text, nullable=False, default="")

    folder_id = Column(String(64), nullable=False, default="")
    category_path = Column(JSONType, default=list)
    wiki_path = Column(String(1024), nullable=False, default="")
    depth = Column(Integer, nullable=False, default=0)
    sort_order = Column(Integer, nullable=False, default=0)

    source_refs = Column(JSONType, default=list)  # [document_id]
    chunk_refs = Column(JSONType, default=list)  # [chunk_id]
    in_links = Column(JSONType, default=list)  # [slug]，反向链接
    out_links = Column(JSONType, default=list)  # [slug]，正向链接
    aliases = Column(JSONType, default=list)
    page_metadata = Column(JSONType, default=dict)

    version = Column(Integer, nullable=False, default=1)
    last_edit_source = Column(String(16), nullable=False, default=WIKI_EDIT_SOURCE_PIPELINE)
    last_editor_id = Column(String(64), nullable=False, default="")

    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="kb_wiki_pages_status_check"
        ),
        # slug 在单个知识库内唯一，跨库可以重复
        Index(
            "idx_kb_wiki_pages_kb_slug",
            "kb_id",
            "slug",
            unique=True,
            postgresql_where=Column("deleted_at").is_(None),
        ),
        Index("idx_kb_wiki_pages_kb_type", "kb_id", "page_type"),
        Index("idx_kb_wiki_pages_folder", "kb_id", "folder_id"),
        Index("idx_kb_wiki_pages_tree", "kb_id", "page_type", "wiki_path", "sort_order", "title"),
        Index("idx_kb_wiki_pages_updated_at", "updated_at"),
    )


class KBWikiJob(Base):
    """Wiki 生成作业。

    Wiki 生成是分钟级到小时级的长任务，挂在请求生命周期上（BackgroundTasks）会在
    容器重启时丢一半，所以状态必须落库：worker 认领后写心跳，重启时按心跳超时回收
    僵尸作业重跑。``progress`` 供前端直接渲染进度，不用另开接口。
    """

    __tablename__ = "kb_wiki_jobs"

    job_id = Column(String(64), primary_key=True)
    kb_id = Column(String(64), ForeignKey("kb_spaces.kb_id", ondelete="CASCADE"), nullable=False)
    job_type = Column(String(20), nullable=False)  # ingest | retract | finalize
    payload = Column(JSONType, default=dict)  # {"document_ids": [...]}
    status = Column(String(20), nullable=False, default="pending")
    attempt = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    last_error = Column(Text)
    progress = Column(JSONType, default=dict)  # {"stage": ..., "done": n, "total": m}

    claimed_by = Column(String(64))
    claimed_at = Column(TIMESTAMP(timezone=True))
    heartbeat_at = Column(TIMESTAMP(timezone=True))

    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    finished_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "job_type IN ('ingest', 'retract', 'finalize')", name="kb_wiki_jobs_type_check"
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="kb_wiki_jobs_status_check",
        ),
        Index("idx_kb_wiki_jobs_status", "status", "created_at"),
        Index("idx_kb_wiki_jobs_kb", "kb_id", "status"),
        # 同一知识库只保留一个未完成的 finalize：多篇文档摄入完只需要收尾一次。
        # 条件里必须带 status，否则一次成功的 finalize 会永久占位、再也排不进下一次。
        #
        # 两个方言的 where 都要给：只写 postgresql_where 时，SQLite 会把谓词整个
        # 丢掉、退化成 UNIQUE(kb_id)——那等于「一个知识库只能有一个作业」，本地
        # 开发和测试库上批量摄入会直接撞唯一约束。
        Index(
            "idx_kb_wiki_jobs_pending_finalize",
            "kb_id",
            unique=True,
            postgresql_where=text("job_type = 'finalize' AND status IN ('pending', 'running')"),
            sqlite_where=text("job_type = 'finalize' AND status IN ('pending', 'running')"),
        ),
    )
