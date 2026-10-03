"""Bounded, sanitized excerpts shared by memory references and evolution traces."""


def sanitized_preview(text: str, *, max_chars: int) -> str:
    """Preserve the existing best-effort sanitizer policy for stored excerpts."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return ""
    try:
        from core.memory.sanitizer import sanitize

        result = sanitize(collapsed)
        if result.reject:
            return "[REDACTED]"
        collapsed = result.text or collapsed
    except Exception:
        pass
    return collapsed[:max_chars]
