"""Knowledge spaces, source documents, assets, chunks and wiki metadata."""
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from sqlalchemy import JSON, BigInteger, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
JSONType = JSON().with_variant(JSONB(), "postgresql")


class KBSpace(Base):
    """Knowledge base space table."""

    __tablename__ = "kb_spaces"

    kb_id = Column(String(64), primary_key=True)
    user_id = Column(
        String(64), ForeignKey("users_shadow.user_id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(255), nullable=False)
    description = Column(Text)
    document_count = Column(Integer, default=0)
    total_size_bytes = Column(BigInteger, default=0)
    visibility = Column(String(16), nullable=False, default="private")
    chunk_method = Column(String(32), nullable=False, default="semantic")
    extra_data = Column("metadata", JSONType, default={})
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at = Column(UTCDateTime(timezone=True))

    # Relationships
    user = relationship("UserShadow")
    documents = relationship("KBDocument", back_populates="kb_space", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("length(name) >= 1 AND length(name) <= 255", name="kb_spaces_name_length"),
        CheckConstraint("document_count >= 0", name="kb_spaces_document_count_check"),
        CheckConstraint("total_size_bytes >= 0", name="kb_spaces_total_size_check"),
        CheckConstraint(
            "visibility IN ('public', 'private', 'scoped')", name="kb_spaces_visibility_check"
        ),
        Index("idx_kb_spaces_user_id", "user_id"),
        Index("idx_kb_spaces_updated_at", "updated_at"),
        Index(
            "idx_kb_spaces_deleted", "deleted_at", postgresql_where=Column("deleted_at").isnot(None)
        ),
        Index("idx_kb_spaces_visibility", "visibility"),
    )

class KBDocument(Base):
    """Knowledge base document table."""

    __tablename__ = "kb_documents"

    document_id = Column(String(64), primary_key=True)
    kb_id = Column(String(64), ForeignKey("kb_spaces.kb_id", ondelete="CASCADE"), nullable=False)
    title = Column(String(500), nullable=False)
    filename = Column(String(500), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    mime_type = Column(String(100), nullable=False)
    storage_key = Column(Text, nullable=False)
    storage_url = Column(Text)
    checksum = Column(String(64))
    indexing_status = Column(
        String(20), nullable=False, default="processing"
    )  # processing | completed | failed
    extra_data = Column("metadata", JSONType, default={})
    uploaded_at = Column(UTCDateTime(timezone=True), default=utc_now)
    deleted_at = Column(UTCDateTime(timezone=True))

    # Relationships
    kb_space = relationship("KBSpace", back_populates="documents")
    chunks = relationship("KBChunk", back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="kb_documents_size_check"),
        CheckConstraint("length(filename) >= 1", name="kb_documents_filename_length"),
        Index("idx_kb_documents_kb_id", "kb_id"),
        Index("idx_kb_documents_uploaded_at", "uploaded_at"),
        Index("idx_kb_documents_kb_uploaded", "kb_id", "uploaded_at"),
        Index(
            "idx_kb_documents_deleted",
            "deleted_at",
            postgresql_where=Column("deleted_at").isnot(None),
        ),
        Index("idx_kb_documents_metadata_gin", "metadata", postgresql_using="gin"),
    )

class KBChunk(Base):
    """Knowledge base chunk table - stores parent chunks for context retrieval.

    Each document is split into parent chunks (stored here) and child chunks
    (vectorised in Milvus hugagent_kb_private collection). Retrieval finds child
    chunks via vector search, then fetches the parent content from this table.
    """

    __tablename__ = "kb_chunks"

    chunk_id = Column(String(64), primary_key=True)
    kb_id = Column(String(64), ForeignKey("kb_spaces.kb_id", ondelete="CASCADE"), nullable=False)
    document_id = Column(
        String(64), ForeignKey("kb_documents.document_id", ondelete="CASCADE"), nullable=False
    )
    chunk_index = Column(Integer, nullable=False)
    content = Column(
        Text, nullable=False
    )  # parent chunk original text, returned to the LLM on retrieval hit
    tags = Column(JSONType, default=list)  # tag list ["数字化转型", "申报条件"]
    questions = Column(JSONType, default=list)  # associated question list (array of strings)
    char_start = Column(Integer)
    char_end = Column(Integer)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    # Relationships
    document = relationship("KBDocument", back_populates="chunks")

    __table_args__ = (
        Index("idx_kb_chunks_kb_id", "kb_id"),
        Index("idx_kb_chunks_document_id", "document_id"),
        Index("idx_kb_chunks_kb_doc", "kb_id", "document_id"),
    )

class KBAsset(Base):
    """Non-text media cut out of a knowledge-base document (图片 / 后续音视频).

    One row per figure the layout engine extracted (or per standalone uploaded image).
    The bytes live in object storage under ``storage_key``; this table holds the
    linkage (which document / which parent chunk), where it sits in the source
    (``locator``), and the two text projections retrieval runs on:

    ``text_content``  literal text carried by the medium — 图注 / OCR 文字；音视频接入后是 ASR 转写。
    ``caption``       语义描述，由 VLM 生成（未配置 vlm 角色时为空，检索退化为只用 text_content）。

    Deliberately medium-agnostic so audio/video need no migration: ``kind`` widens,
    ``locator`` holds ``{page_idx, bbox}`` for images and would hold ``{start_ms,
    end_ms}`` for a transcript segment.

    ``vector_state`` records which vector spaces this asset has been indexed into
    (``{"text": "ok"}`` today). Path B — a true multimodal embedding in its own
    collection — adds a second key here instead of another column.
    """

    __tablename__ = "kb_assets"

    asset_id = Column(String(64), primary_key=True)
    kb_id = Column(String(64), ForeignKey("kb_spaces.kb_id", ondelete="CASCADE"), nullable=False)
    document_id = Column(
        String(64), ForeignKey("kb_documents.document_id", ondelete="CASCADE"), nullable=False
    )
    # Owning parent chunk. Nullable: an asset whose placeholder cannot be located in
    # any chunk is still worth keeping (it stays retrievable on its own row).
    chunk_id = Column(String(64))
    kind = Column(String(16), nullable=False, default="image")  # image | audio | video
    mime_type = Column(String(100), nullable=False, default="application/octet-stream")
    storage_key = Column(Text, nullable=False)
    size_bytes = Column(BigInteger, default=0)
    asset_index = Column(Integer, nullable=False, default=0)  # order within the document
    locator = Column(JSONType, default=dict)  # image: {page_idx, bbox}; av: {start_ms, end_ms}
    caption = Column(Text)  # VLM 语义描述
    text_content = Column(Text)  # 图注 / OCR / ASR 转写
    caption_status = Column(
        String(20), nullable=False, default="pending"
    )  # pending | completed | skipped | failed
    vector_state = Column(JSONType, default=dict)  # {"text": "ok"} —— 路径 B 在此加键
    extra_data = Column("metadata", JSONType, default={})
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint("kind IN ('image', 'audio', 'video')", name="kb_assets_kind_check"),
        Index("idx_kb_assets_kb_id", "kb_id"),
        Index("idx_kb_assets_document_id", "document_id"),
        Index("idx_kb_assets_chunk_id", "chunk_id"),
    )

WIKI_CATEGORY_MAX_DEPTH = 3

WIKI_PAGE_TYPE_SUMMARY = "summary"

WIKI_PAGE_TYPE_ENTITY = "entity"

WIKI_PAGE_TYPE_CONCEPT = "concept"

WIKI_PAGE_TYPE_INDEX = "index"

WIKI_PAGE_TYPE_SYNTHESIS = "synthesis"

WIKI_PAGE_TYPE_COMPARISON = "comparison"

WIKI_PAGE_TYPES = (
    WIKI_PAGE_TYPE_SUMMARY,
    WIKI_PAGE_TYPE_ENTITY,
    WIKI_PAGE_TYPE_CONCEPT,
    WIKI_PAGE_TYPE_INDEX,
    WIKI_PAGE_TYPE_SYNTHESIS,
    WIKI_PAGE_TYPE_COMPARISON,
)

WIKI_KNOWLEDGE_PAGE_TYPES = (
    WIKI_PAGE_TYPE_ENTITY,
    WIKI_PAGE_TYPE_CONCEPT,
    WIKI_PAGE_TYPE_SYNTHESIS,
    WIKI_PAGE_TYPE_COMPARISON,
)

WIKI_PAGE_TYPE_LABELS = {
    WIKI_PAGE_TYPE_CONCEPT: "概念",
    WIKI_PAGE_TYPE_ENTITY: "实体",
    WIKI_PAGE_TYPE_SYNTHESIS: "综述",
    WIKI_PAGE_TYPE_COMPARISON: "对比",
    WIKI_PAGE_TYPE_SUMMARY: "文档摘要",
    WIKI_PAGE_TYPE_INDEX: "总索引",
}

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
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at = Column(UTCDateTime(timezone=True))

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

    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    deleted_at = Column(UTCDateTime(timezone=True))

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
    claimed_at = Column(UTCDateTime(timezone=True))
    heartbeat_at = Column(UTCDateTime(timezone=True))

    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    finished_at = Column(UTCDateTime(timezone=True))

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

# Register with the schema, including when the installed feature is runtime-disabled.
def _cleanup_user_knowledge(session, user):
    for space in session.query(KBSpace).filter(KBSpace.user_id == user.user_id):
        session.delete(space)


from core.harness.registry import register_user_cleanup

register_user_cleanup(_cleanup_user_knowledge)
