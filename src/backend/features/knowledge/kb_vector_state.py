"""Milvus collection names, field size limits and per-process collection cache for the private KB."""
from __future__ import annotations
from typing import Optional

# Pre-HNSW collection: IVF_FLAT, no partition key. Still used on Milvus Lite and on
# servers that have not run ``python -m features.knowledge.vector_maintenance migrate``.
LEGACY_COLLECTION_NAME = "hugagent_kb_private"

# Current server collection: HNSW dense index, ``kb_id`` as partition key.
CURRENT_COLLECTION_NAME = "hugagent_kb_private_v2"

# Collection this process reads and writes. Resolved once per process by
# ``vector_client.resolve_collection``; everything reads it at call time.
COLLECTION_NAME = LEGACY_COLLECTION_NAME

# "hnsw" | "ivf" | "lite": the active collection's kind; None until resolved.
ACTIVE_INDEX: Optional[str] = None

# True once the active collection is known to exist, so deletes and lookups skip
# has_collection. VERIFIED_COLLECTION_DIM (below) additionally means its dimension
# was checked, so writes and searches skip describe_collection too.
COLLECTION_EXISTS = False

PARTITION_COUNT = 64


TITLE_FIELD_MAX_BYTES = 500


CONTENT_FIELD_MAX_BYTES = 4096


TAGS_FIELD_MAX_BYTES = 1000


# Text the current collection's BM25 function tokenises (chunk text + tags). Holds the
# whole child/flat chunk, so it is larger than the truncated ``content`` payload.
SPARSE_TEXT_FIELD_MAX_BYTES = 32768

# jieba segments Chinese (and splits Latin words); lowercase makes "Milvus" match
# "milvus"; cnalphanumonly drops punctuation tokens.
SPARSE_ANALYZER_PARAMS = {"tokenizer": "jieba", "filter": ["lowercase", "cnalphanumonly"]}


VERIFIED_COLLECTION_DIM: Optional[int] = None


SPARSE_DIM_SPACE = 100_000
