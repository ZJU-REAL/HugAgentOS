"""kb state responsibilities."""
from __future__ import annotations


# Shared (team / public) KB documents. Private KBs have no fixed cap: they follow the
# configured personal_kb_upload_limit_mb (role / team / user permissions).
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB


MAX_PREVIEW_SIZE = 20 * 1024 * 1024  # 20MB
