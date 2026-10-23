"""Intervals and patterns used while streaming."""
from __future__ import annotations
import re

HTTP_ERR_RE = re.compile(r"HTTP\s*[45]\d\d")


MODEL_PROGRESS_MIN_INTERVAL_S = 5.0


QUEUE_POLL_INTERVAL_S = 3.0


TOOL_CALL_DELTA_FLUSH_INTERVAL_S = 0.05


TOOL_CALL_DELTA_FLUSH_CHARS = 256
