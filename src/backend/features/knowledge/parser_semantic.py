"""parser semantic responsibilities."""
from __future__ import annotations
import math
from typing import Callable, Optional
from features.knowledge import kb_parser_state
from features.knowledge import parser_chunks
from features.knowledge import parser_tokens
from features.knowledge import parser_types

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors (pure Python)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def build_embedding_semantic_chunks(
    paragraphs: list[dict],
    parent_size: int,
    child_size: int,
    overlap: int,
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    separators: Optional[list[str]] = None,
) -> list[parser_types.ParentChunk]:
    """Semantic chunking based on embedding similarity between adjacent blocks.

    Algorithm:
    1. Merge paragraphs into minimum ~50-token blocks
    2. Batch-embed all blocks
    3. Compute cosine similarity between adjacent blocks
    4. Set breakpoints where similarity drops below P25 or is a local minimum
    5. Merge blocks between breakpoints into parent chunks
    6. Fallback to recursive splitting for oversized parents
    """
    if embed_fn is None:
        # Fallback to structured chunking when no embed function available
        return parser_chunks.build_structured_chunks(paragraphs, parent_size, child_size, overlap)

    # Step 1: Merge paragraphs into minimum-size blocks (~50 tokens)
    MIN_BLOCK_TOKENS = 50
    blocks: list[str] = []
    current_block = ""
    current_tokens = 0

    for para in paragraphs:
        text = para["text"]
        tokens = parser_tokens.count_tokens(text)
        if current_block and current_tokens + tokens > MIN_BLOCK_TOKENS:
            blocks.append(current_block)
            current_block = text
            current_tokens = tokens
        else:
            current_block = (current_block + "\n" + text) if current_block else text
            current_tokens += tokens

    if current_block:
        blocks.append(current_block)

    if len(blocks) <= 1:
        # Single block — just use structured
        return parser_chunks.build_structured_chunks(paragraphs, parent_size, child_size, overlap)

    # Step 2: Batch embed
    embeddings = embed_fn(blocks)

    # Step 3: Compute adjacent cosine similarities
    similarities: list[float] = []
    for i in range(len(embeddings) - 1):
        sim = cosine_similarity(embeddings[i], embeddings[i + 1])
        similarities.append(sim)

    if not similarities:
        return parser_chunks.build_structured_chunks(paragraphs, parent_size, child_size, overlap)

    # Step 4: Find breakpoints
    sorted_sims = sorted(similarities)
    p25_idx = max(0, len(sorted_sims) // 4 - 1)
    p25_threshold = sorted_sims[p25_idx]

    breakpoints: set[int] = set()
    for i, sim in enumerate(similarities):
        # Below P25 threshold
        if sim <= p25_threshold:
            breakpoints.add(i)
            continue
        # Local minimum: lower than both neighbors
        is_local_min = True
        if i > 0 and sim >= similarities[i - 1]:
            is_local_min = False
        if i < len(similarities) - 1 and sim >= similarities[i + 1]:
            is_local_min = False
        if is_local_min and sim < p25_threshold * 1.2:
            breakpoints.add(i)

    # Step 5: Merge blocks between breakpoints into parent chunks
    result: list[parser_types.ParentChunk] = []
    segment_start = 0

    sorted_breaks = sorted(breakpoints)
    # Add an artificial breakpoint at the end
    sorted_breaks.append(len(blocks) - 1)

    for bp in sorted_breaks:
        segment_blocks = blocks[segment_start:bp + 1]
        parent_text = "\n".join(segment_blocks).strip()
        segment_start = bp + 1

        if not parent_text:
            continue

        # Step 6: If parent is too large, use recursive split as fallback
        if parser_tokens.count_tokens(parent_text) > parent_size * 1.5:
            sub_segments = parser_chunks.recursive_split(parent_text, separators or kb_parser_state.RECURSIVE_SEPARATORS, parent_size)
            for sub in sub_segments:
                sub = sub.strip()
                if sub:
                    result.append(parser_chunks.make_parent_chunk(sub, parser_chunks.split_into_children(sub, child_size, overlap)))
        else:
            result.append(parser_chunks.make_parent_chunk(parent_text, parser_chunks.split_into_children(parent_text, child_size, overlap)))

    # Handle remaining blocks
    if segment_start < len(blocks):
        remaining = "\n".join(blocks[segment_start:]).strip()
        if remaining:
            result.append(parser_chunks.make_parent_chunk(remaining, parser_chunks.split_into_children(remaining, child_size, overlap)))

    return result
