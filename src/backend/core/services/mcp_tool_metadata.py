"""Capture the same public MCP schema for discovery, hashing and desktop delivery."""
import hashlib
import json
from core.services.desktop_capability_protocol import public_tool_schema


def snapshot_tool(tool):
    raw = getattr(tool, "_tool", tool)
    if hasattr(raw, "model_dump"):
        data = raw.model_dump(by_alias=True, mode="json")
    else:
        data = {"name": tool.name, "description": getattr(tool, "description", ""),
                "inputSchema": getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", {})}
    return public_tool_schema(data)


def tool_snapshot_hash(tools):
    normalized = sorted([public_tool_schema(t) for t in tools or []], key=lambda t: t["name"])
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
