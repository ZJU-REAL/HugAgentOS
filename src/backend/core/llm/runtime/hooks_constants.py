"""Limits for file content injected by context hooks."""
from __future__ import annotations
import re

MAX_FILE_CONTENT_CHARS = 50_000  # per-turn read_artifact budget per file


FILE_ID_RE = re.compile(r'"file_id"\s*:\s*"([A-Za-z0-9_\-]+)"')


PIN_HINT_SKIP_TOOLS: frozenset[str] = frozenset(
    {
        "pin_to_workspace",  # the pin tool itself
        "read_artifact",  # echoes the input file_id; reading, not producing
        "generate_response",  # the ReActAgent finish function
    }
)
