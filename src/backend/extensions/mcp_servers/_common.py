"""Common utilities for MCP servers."""

from __future__ import annotations

from typing import Callable


def safe_stream_writer() -> Callable[[str], None]:
    """Get a safe stream writer that won't fail outside runnable context.

    Returns:
        A callable that writes to stream if available, or does nothing silently.
    """
    # After AgentScope migration, MCP servers run as independent stdio
    # processes without a LangGraph stream context. Return a no-op.
    return lambda msg: None
