"""Plan creation: dispatches to backend internal API to resolve data sources.

The MCP tool runs in a separate stdio process and cannot access the DB
directly. We HTTP-call back to the backend's internal endpoint
``POST /v1/internal/batch/resolve`` which:
  - parses uploaded files (xlsx → rows, word → content)
  - splits natural-language enumeration into items (LLM call)
  - infers a default prompt template
  - persists the plan to the batch_plans table
  - returns {plan_id, total, preview, ...}
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import httpx


def _backend_url() -> str:
    """Resolve the backend internal URL.

    Priority:
      1. ``BACKEND_INTERNAL_URL`` if set (explicit override, e.g. when MCP
         runs in a sibling container and needs ``http://backend:PORT``).
      2. ``http://127.0.0.1:${BACKEND_PORT}`` — works for the default
         setup where the MCP stdio subprocess is launched inside the
         backend container, so localhost == the backend itself.
         127.0.0.1 (not ``localhost``) avoids IPv6 ::1 resolution traps.
      3. ``http://127.0.0.1:3001`` as a last-resort default.
    """
    explicit = os.environ.get("BACKEND_INTERNAL_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = (os.environ.get("BACKEND_PORT") or "3001").strip() or "3001"
    return f"http://127.0.0.1:{port}"


def _internal_token() -> str:
    return os.environ.get("BACKEND_INTERNAL_TOKEN", "")


async def create_plan(
    *,
    instruction: str,
    file_ids: List[str],
    text_items: List[str],
    chat_id: str,
) -> Dict[str, Any]:
    """Call backend internal resolver to create a batch plan."""
    if not instruction and not text_items and not file_ids:
        return {
            "error": "batch_plan 需要 instruction 或 text_items 或 file_ids 至少一项",
            "result": None,
        }

    payload = {
        "instruction": instruction,
        "file_ids": file_ids,
        "text_items": text_items,
        "chat_id": chat_id,
    }

    headers = {"Content-Type": "application/json"}
    token = _internal_token()
    if token:
        headers["X-Internal-Token"] = token

    url = f"{_backend_url()}/v1/internal/batch/resolve"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                return {
                    "error": f"batch_plan 后端解析失败 status={resp.status_code}: {resp.text[:300]}",
                    "result": None,
                }
            data = resp.json()
            # Internal resolver returns the plan dict directly (not wrapped in envelope).
            if isinstance(data, dict) and "plan_id" in data:
                return data
            # If envelope-wrapped, unwrap.
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                return data["data"]
            return {"error": f"batch_plan 后端返回异常: {data}", "result": None}
    except Exception as e:
        return {
            "error": f"batch_plan 调用后端失败: {e}",
            "result": None,
        }
