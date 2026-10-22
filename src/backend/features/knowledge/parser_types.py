"""parser types responsibilities."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ChildChunk:
    child_id: str
    content: str
    index: int          # position within parent


@dataclass
class ParentChunk:
    parent_id: str
    content: str        # full parent text, returned to LLM on retrieval
    children: list[ChildChunk]
    char_start: Optional[int] = None
    char_end: Optional[int] = None


@dataclass
class RawAsset:
    """A non-text medium extracted from the source document, before persistence.

    ``placeholder`` is the exact token this medium is referenced by inside the
    paragraph text (``images/<sha>.jpg`` for a layout-extracted figure, a synthetic
    ``asset://<uuid>`` for a standalone upload). It is what links the medium back to
    the parent chunk that contains it, and what gets rewritten to a real URL once the
    asset has an id.

    Medium-agnostic on purpose: ``kind`` widens to audio/video, and ``locator`` holds
    ``{page_idx, bbox}`` for an image where a transcript segment would hold
    ``{start_ms, end_ms}``.
    """

    data: bytes
    placeholder: str
    kind: str = "image"
    mime_type: str = "application/octet-stream"
    text_content: str = ""          # 图注 / OCR（音视频接入后：ASR 转写）
    locator: dict = field(default_factory=dict)
    index: int = 0
