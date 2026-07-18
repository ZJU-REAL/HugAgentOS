"""Layered memory extractors.

Splits the former 140-line `custom_fact_extraction_prompt` into four dedicated extractors:

- `IDENTITY` → user identity tuple (name / organization / department / role) → written to L1 Profile
- `PREFERENCE` → stable preferences (format / verbosity / style / taboos) → written to L1 Profile
- `FACT` → business facts (queried data / business entities / methodological conclusions) → written to L2 Milvus
- `TASK` → session task working set → written to the Session auxiliary layer

Routing: `router.py::classify_conversation()` classifies by keyword; an empty match set is skipped outright.
Scheduling: `router.py::run_extractors_with_timeout()` runs the matched extractors concurrently, each with its own timeout.
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
