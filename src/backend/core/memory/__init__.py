"""Layered memory system — unified package entry point.

Layered by information stability and access frequency:
- **L1 Profile** (`profile`): bounded markdown profile, frozen-injected at session start
- **L2 Fact** (`service`): mem0 + Milvus vector facts, retrieved on demand
- **L3 Graph** (via `service`/Neo4j): relationship graph, invoked on demand via tools
- **Session helper layer** (`chats.metadata.session_memory`): per-session task working set
- **Audit sidechannel** (`audit`): audit trail for all reads and writes

Non-blocking guarantee: memory I/O is never synchronously awaited on the main SSE path.
- Retrieval: `launch_memory_retrieval` background task + `wait_for` budget
- Saving: `schedule_post_response_tasks` bounded-semaphore fire-and-forget

Public API (downstream should import from `core.memory`):
"""

from core.memory.context import (
    MemoryContext,
    resolve_workspace_id,
    resolve_allowed_levels,
)
from core.memory.sanitizer import (
    SanitizeResult,
    sanitize,
    invalidate_rules_cache,
    REDACT_PATTERNS,
    CLASSIFIED_TERMS,
)
from core.memory.audit import record as audit_record
from core.memory.audit import record_batch as audit_record_batch
from core.memory.pipeline import (
    CircuitBreaker,
    milvus_breaker,
    schedule_post_response_tasks,
    get_background_semaphore,
)

__all__ = [
    # context
    "MemoryContext",
    "resolve_workspace_id",
    "resolve_allowed_levels",
    # sanitizer
    "SanitizeResult",
    "sanitize",
    "invalidate_rules_cache",
    "REDACT_PATTERNS",
    "CLASSIFIED_TERMS",
    # audit
    "audit_record",
    "audit_record_batch",
    # pipeline
    "CircuitBreaker",
    "milvus_breaker",
    "schedule_post_response_tasks",
    "get_background_semaphore",
]
