"""Knowledge-base document indexing-status normalization and aggregation."""

from collections.abc import Iterable, Mapping
from typing import Any, Literal

DocumentStatusCategory = Literal["indexed", "processing", "failed"]

_PROCESSING_STATUSES = {
    "processing",
    "indexing",
    "waiting",
    "pending",
    "finalizing",
    "parsing",
    "queued",
    "queuing",
    "waiting_indexing",
    "paused",
}
_FAILED_STATUSES = {"failed", "error"}


def classify_document_status(status: Any) -> DocumentStatusCategory:
    """Map local and external-provider statuses to the three UI categories."""
    normalized = str(status or "").strip().lower()
    if normalized in _PROCESSING_STATUSES:
        return "processing"
    if normalized in _FAILED_STATUSES:
        return "failed"
    return "indexed"


def empty_document_status_counts() -> dict[str, int]:
    return {"indexed": 0, "processing": 0, "failed": 0}


def summarize_document_statuses(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count normalized indexing statuses from document-shaped mappings."""
    counts = empty_document_status_counts()
    for item in items:
        counts[classify_document_status(item.get("indexing_status"))] += 1
    return counts
