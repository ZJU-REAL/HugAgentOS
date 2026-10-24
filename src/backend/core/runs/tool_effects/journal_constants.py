"""Retention and redaction settings for the tool effect journal."""
from __future__ import annotations
import re

ATTENTION_RETENTION_FACTOR = 4


REDACT_FIELDS = [
    "password",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "access_key",
    "cookie",
]


WITHHOLD_FIELDS = {"command", "content", "stdin", "script", "code"}


EMBEDDED_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s'\";]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)([?&](?:token|api_key|apikey|access_key|secret|password|passwd|client_secret|authorization)=)[^&#\s]+"
    ),
    re.compile(
        r"(?i)((?:token|api_key|apikey|access_key|secret|password|passwd|client_secret|authorization)\s*[=:]\s*)[^\s'\";]+"
    ),
)
