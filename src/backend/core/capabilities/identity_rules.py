"""Capability kinds and portable identity segments, independent of host storage."""
import re

KIND_SKILL = "skill"

KIND_PLUGIN = "plugin"

KIND_AGENT = "agent"

KIND_MCP = "mcp"

KINDS = (KIND_SKILL, KIND_PLUGIN, KIND_AGENT, KIND_MCP)

LOCAL_PROFILE = "local"

BUILTIN_PROFILE = "builtin"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

def safe_segment(value: str) -> str:
    """A single path segment safe on every platform, or ``ValueError``."""
    seg = (value or "").strip()
    if seg != value or seg.endswith((".", " ")) or not _SEGMENT_RE.match(seg) or seg in (".", ".."):
        raise ValueError(f"unsafe path segment: {value!r}")
    if seg.split(".")[0].upper() in _WINDOWS_RESERVED:
        raise ValueError(f"reserved name: {value!r}")
    return seg
