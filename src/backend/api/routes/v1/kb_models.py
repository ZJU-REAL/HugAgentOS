"""Community knowledge-base request models."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class IndexingConfig(BaseModel):
    parent_chunk_size: int = Field(1024, ge=256, le=4096)
    child_chunk_size: int = Field(128, ge=64, le=512)
    overlap_tokens: int = Field(20, ge=0, le=100)
    parent_child_indexing: bool = True
    auto_keywords_count: int = Field(0, ge=0, le=10)
    auto_questions_count: int = Field(0, ge=0, le=5)
    separators: Optional[List[str]] = None
    child_separators: Optional[List[str]] = None


class CreateKBSpaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    chunk_method: Optional[str] = "semantic"
    metadata: Optional[dict] = Field(default_factory=dict)
    indexing_config: Optional[IndexingConfig] = None
    visibility: Literal["private"] = "private"


class UpdateKBSpaceRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None


class PolishKBDescriptionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class UpdateChunkRequest(BaseModel):
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    questions: Optional[List[str]] = None


class ReindexRequest(BaseModel):
    indexing_config: Optional[IndexingConfig] = None
    chunk_method: Optional[str] = None


__all__ = [
    "CreateKBSpaceRequest",
    "IndexingConfig",
    "PolishKBDescriptionRequest",
    "ReindexRequest",
    "UpdateChunkRequest",
    "UpdateKBSpaceRequest",
]
