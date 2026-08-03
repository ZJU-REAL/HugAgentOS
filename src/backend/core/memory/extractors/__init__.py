"""Layered memory extractors.

- `IDENTITY` → user identity tuple (name / organization / department / role) → written to L1 Profile
- `PREFERENCE` → stable preferences (format / verbosity / style / taboos) → written to L1 Profile
- `PROCEDURAL` → how work is done here (orderings, definitions, checks, red lines) → written to L2 Milvus
- `TASK` → session task working set → written to the Session auxiliary layer

**There is no FACT extractor.** L2 used to store business facts, and that was
the layer's central mistake: a fact is a snapshot that begins decaying the
moment it is written, so remembering it means confidently recalling a stale
number instead of looking the current one up. What survives repetition is
procedure — and it is also the only memory a skill can be compiled from.

Routing: `router.py::classify_conversation()` decides what runs. Identity,
preference and task are keyword-gated; **procedural runs on every substantive
turn**, because a convention can be stated in any phrasing and a regex deciding
before the model ever reads the turn is exactly how procedures were missed.
"""

from core.memory.extractors.router import (
    ExtractorType,
    classify_conversation,
    run_extractors_with_timeout,
)

__all__ = [
    "ExtractorType",
    "classify_conversation",
    "run_extractors_with_timeout",
]
