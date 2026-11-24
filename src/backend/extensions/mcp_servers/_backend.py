"""Callback helpers for MCP servers that forward work to the backend's internal API."""

from __future__ import annotations

import os


def backend_url() -> str:
    """Base URL of the backend: ``BACKEND_INTERNAL_URL`` → ``BACKEND_PORT`` → ``PORT`` → 3001.

    Compose sets ``BACKEND_INTERNAL_URL``; local child MCP processes inherit ``PORT``
    from ``hugagent serve`` (the desktop app listens on 32101).
    """
    explicit = os.environ.get("BACKEND_INTERNAL_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = (os.environ.get("BACKEND_PORT") or os.environ.get("PORT") or "3001").strip() or "3001"
    return f"http://127.0.0.1:{port}"


def internal_token() -> str:
    return os.environ.get("BACKEND_INTERNAL_TOKEN", "")


def internal_json_headers() -> dict[str, str]:
    """JSON headers plus ``X-Internal-Token`` when a shared secret is configured."""
    headers = {"Content-Type": "application/json"}
    token = internal_token()
    if token:
        headers["X-Internal-Token"] = token
    return headers
