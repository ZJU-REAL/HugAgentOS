"""vector fields responsibilities."""
from __future__ import annotations
from features.knowledge import kb_vector_state

def truncate_utf8(text: str, max_bytes: int = kb_vector_state.CONTENT_FIELD_MAX_BYTES) -> str:
    """Safe truncation by UTF-8 bytes (Chinese is 3 bytes/char); never cuts in the middle of a multi-byte character.

    Historical bug: using ``text[:4096]`` truncated by "character"; 4096 Chinese chars ≈ 12KB bytes, still exceeding Milvus's
    4096 **byte** limit → ``length exceeds max length`` error.
    """
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")
