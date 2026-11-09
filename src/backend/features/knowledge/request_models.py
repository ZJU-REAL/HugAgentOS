"""Pydantic request models for Knowledge Base routes.

The single definition for both editions: CE ships this file unchanged, so the
routes can never read a field its edition's copy lacks. Edition differences in
*values* (CE accepts only private spaces) are enforced in the routes; fields
that only edition-only routes read live beside those routes in ``edition_ee``.
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class IndexingConfig(BaseModel):
    """Indexing configuration for KB space."""
    parent_chunk_size: int = Field(1024, ge=256, le=4096, description="Parent chunk size in chars")
    child_chunk_size: int = Field(128, ge=64, le=512, description="Child chunk size in chars")
    overlap_tokens: int = Field(20, ge=0, le=100, description="Overlap between child chunks")
    parent_child_indexing: bool = Field(True, description="Enable parent-child indexing; False = index parent chunks only")
    auto_keywords_count: int = Field(0, ge=0, le=10, description="LLM auto-extract keyword count per chunk (0=disabled)")
    auto_questions_count: int = Field(0, ge=0, le=5, description="LLM auto-generate question count per chunk (0=disabled)")
    separators: Optional[List[str]] = Field(
        None,
        description="父分块分隔符层级（仅对递归分块及语义分块的递归兜底生效）；为空用内置默认层级",
    )
    child_separators: Optional[List[str]] = Field(
        None,
        description="子分块分隔符层级（父子分块时按此切子块，再按 child_size 打包）；为空走定长滑窗",
    )


class WikiConfigRequest(BaseModel):
    """Wiki 生成参数。仅在 index_modes 含 wiki 时有意义。"""
    granularity: Optional[str] = Field(
        "standard",
        description="抽取粒度：focused（仅 3-7 个主题）| standard（默认，均衡）| exhaustive（穷举术语表）",
    )
    language: Optional[str] = Field("中文", description="Wiki 页面撰写语言")
    map_parallel: Optional[int] = Field(None, ge=1, le=16, description="抽取阶段并发")
    reduce_parallel: Optional[int] = Field(None, ge=1, le=16, description="写页阶段并发")
    citation_chunk_batch: Optional[int] = Field(None, ge=1, le=40, description="每批引文标注的分块数")
    max_llm_calls: Optional[int] = Field(
        None, ge=1, le=100000, description="单次生成的模型调用上限，防止大库打空额度"
    )
    max_chunks_per_page: Optional[int] = Field(None, ge=1, le=60, description="单页最多保留的引文分块数")
    extraction_instructions: Optional[str] = Field(
        None,
        max_length=4000,
        description=(
            "抽取要求：这个库里什么算重要的条目、该重点关注哪些领域概念。"
            "只影响抽什么，不能改写输出格式与引用接地规则（系统规则优先）"
        ),
    )
    content_instructions: Optional[str] = Field(
        None,
        max_length=4000,
        description=(
            "撰写要求：条目页的语气、结构与详略取舍。"
            "只影响怎么写，不能改写输出格式与引用接地规则（系统规则优先）"
        ),
    )


class CreateKBSpaceRequest(BaseModel):
    """Request model for creating a KB space."""
    name: str = Field(..., min_length=1, max_length=255, description="KB space name")
    description: Optional[str] = Field(None, description="KB space description")
    chunk_method: Optional[str] = Field("semantic", description="Chunking strategy: semantic|laws|qa")
    metadata: Optional[dict] = Field(default_factory=dict, description="Additional metadata")
    indexing_config: Optional[IndexingConfig] = Field(None, description="Advanced indexing configuration")
    index_modes: Optional[List[str]] = Field(
        None,
        description=(
            "索引模式：rag（向量检索）| wiki（LLM Wiki 图谱），可多选。"
            "两者同选即 LLM-Wiki 知识库。缺省为 ['rag']。"
            "注意三种模式都会分块——wiki 省掉的是向量化，不是分块。"
        ),
    )
    wiki_config: Optional[WikiConfigRequest] = Field(None, description="Wiki 生成参数")
    visibility: Optional[str] = Field("private", description="private（仅本人）| public（共享，按授权可见；CE 只接受 private）")


class UpdateKBSpaceRequest(BaseModel):
    """Request model for updating a KB space."""
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="KB space name")
    description: Optional[str] = Field(None, description="KB space description")
    index_modes: Optional[List[str]] = Field(
        None, description="索引模式：rag | wiki，可多选。新勾选 wiki 后需调用重新生成接口补建已有文档"
    )
    wiki_config: Optional[WikiConfigRequest] = Field(None, description="Wiki 生成参数")


class PolishKBDescriptionRequest(BaseModel):
    """Request model for AI-polishing a KB description."""
    name: str = Field(..., min_length=1, max_length=255, description="KB space name")
    description: Optional[str] = Field(None, description="Current KB description")


class UpdateChunkRequest(BaseModel):
    """Request model for updating chunk content, tags and questions."""
    content: Optional[str] = Field(None, description="Full chunk text (admin manual edit; triggers re-embedding)")
    tags: Optional[List[str]] = Field(None, description="Tag list for BM25 augmentation")
    questions: Optional[List[str]] = Field(None, description="Question list for multi-surface indexing")


class ReindexRequest(BaseModel):
    """Optional indexing config override for reindexing."""
    indexing_config: Optional[IndexingConfig] = Field(None, description="Override indexing configuration")
    chunk_method: Optional[str] = Field(None, description="Override chunking method: structured|recursive|embedding_semantic|laws|qa")
