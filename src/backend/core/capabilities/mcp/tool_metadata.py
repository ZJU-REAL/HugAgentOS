"""Capture the same public MCP schema for discovery, hashing and desktop delivery."""
import hashlib
import json
from core.capabilities.manifest_protocol import public_tool_schema


def snapshot_tool(tool):
    return public_tool_schema(tool.model_dump(by_alias=True, mode="json"))


def tool_snapshot_hash(tools):
    normalized = sorted([public_tool_schema(t) for t in tools or []], key=lambda t: t["name"])
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
