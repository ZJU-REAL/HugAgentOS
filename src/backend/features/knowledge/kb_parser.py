"""kb parser responsibilities."""
from __future__ import annotations
from typing import Callable, Optional
from features.knowledge import parser_chunks
from features.knowledge import parser_extract
from features.knowledge import parser_semantic
from features.knowledge import parser_types

def build_parent_child_chunks(
    paragraphs: list[dict],
    parent_size: int = 1024,
    child_size: int = 128,
    overlap: int = 20,
    chunk_method: str = "semantic",
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    separators: Optional[list[str]] = None,
    child_separators: Optional[list[str]] = None,
) -> list[parser_types.ParentChunk]:
    """Build parent-child chunk pairs from extracted paragraphs.

    Parent chunks: large semantic units (~parent_size tokens), stored in PostgreSQL,
                   returned to LLM as full context.
    Child chunks: small slices (~child_size tokens), vectorised in Milvus for retrieval.

    ``separators`` (parent-chunk separators): custom separator hierarchy that decides
    parent-chunk boundaries; only effective on paths that go through recursive
    splitting (the recursive method itself, and the recursive fallback of semantic
    chunking for oversized parents); when empty, falls back to the default hierarchy.
    ``child_separators`` (child-chunk separators): when a parent is split into
    children, split on these separators (then pack by child_size); when empty,
    children use the default fixed-length sliding-window split. The two are
    independent and do not affect each other.
    """
    seps = parser_chunks.normalize_separators(separators)
    csep = parser_chunks.normalize_separators(child_separators)
    if chunk_method == "qa":
        parents = parser_chunks.build_qa_chunks(paragraphs)
    elif chunk_method == "laws":
        parents = parser_chunks.build_law_chunks(paragraphs, parent_size, child_size, overlap)
    elif chunk_method == "recursive":
        parents = parser_chunks.build_recursive_chunks(paragraphs, parent_size, child_size, overlap, separators=seps)
    elif chunk_method == "embedding_semantic":
        parents = parser_semantic.build_embedding_semantic_chunks(
            paragraphs, parent_size, child_size, overlap, embed_fn=embed_fn, separators=seps,
        )
    else:
        # "semantic" / "structured" / default
        parents = parser_chunks.build_structured_chunks(paragraphs, parent_size, child_size, overlap)

    # Child-chunk separators: re-split children uniformly after parents are built
    # (overriding each builder's default fixed-length children), so we don't have to
    # thread the same parameter into every builder — one post-processing step covers
    # all chunking methods.
    if csep:
        parser_chunks.apply_child_separators(parents, child_size, csep)
    return parents


def parse_and_chunk(
    file_bytes: bytes,
    mime_type: str,
    chunk_method: str = "semantic",
    parent_size: int = 1024,
    child_size: int = 128,
    overlap: int = 20,
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    separators: Optional[list[str]] = None,
    child_separators: Optional[list[str]] = None,
) -> list[parser_types.ParentChunk]:
    """Full pipeline: extract text then build parent-child chunks."""
    parents, _ = parse_and_chunk_rich(
        file_bytes,
        mime_type,
        chunk_method=chunk_method,
        parent_size=parent_size,
        child_size=child_size,
        overlap=overlap,
        embed_fn=embed_fn,
        separators=separators,
        child_separators=child_separators,
        with_assets=False,
    )
    return parents


def parse_and_chunk_rich(
    file_bytes: bytes,
    mime_type: str,
    chunk_method: str = "semantic",
    parent_size: int = 1024,
    child_size: int = 128,
    overlap: int = 20,
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    separators: Optional[list[str]] = None,
    child_separators: Optional[list[str]] = None,
    with_assets: bool = True,
) -> tuple[list[parser_types.ParentChunk], list[parser_types.RawAsset]]:
    """Same pipeline as :func:`parse_and_chunk`, also returning the document's media.

    The assets are returned unpersisted; the caller links them to parent chunks and
    stores the bytes (see ``core/kb/kb_assets.py``).
    """
    paragraphs, assets = parser_extract.extract_text_with_assets(
        file_bytes, mime_type, with_assets=with_assets
    )
    parents = build_parent_child_chunks(
        paragraphs,
        parent_size=parent_size,
        child_size=child_size,
        overlap=overlap,
        chunk_method=chunk_method,
        embed_fn=embed_fn,
        separators=separators,
        child_separators=child_separators,
    )
    return parents, assets
