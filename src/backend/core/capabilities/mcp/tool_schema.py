"""Interpret server tool schema metadata without loading the SDK."""
from typing import Any

def has_usable_schema(tool: Any) -> bool:
    """Whether a manifest tool entry carries the server's real ``inputSchema``.

    A tool the platform only knows by name and description (a plugin manifest
    lists its tools for display, without schemas) must not be handed to a model:
    the parameter object would be empty, so the model has nowhere to put its
    arguments and every call arrives blank.

    The marker is whether a schema was captured at all, not whether it declares
    parameters: a server is free to describe an argument-less tool as
    ``{"type": "object"}``, while an entry that never reached the server carries
    no ``inputSchema`` key, which normalizes to an empty dict.
    """
    if not isinstance(tool, dict):
        return False
    schema = tool.get("inputSchema") or tool.get("input_schema")
    return isinstance(schema, dict) and bool(schema)
