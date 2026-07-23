"""Community-edition site publishing MCP implementation."""

from __future__ import annotations

import os
from typing import Any, Dict

import httpx


def _backend_url() -> str:
    explicit = os.environ.get("BACKEND_INTERNAL_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    # Desktop/local mode has no nginx or compose service name.  Its MCP child
    # inherits the backend listener through PORT (32101 by default).
    port = (os.environ.get("BACKEND_PORT") or os.environ.get("PORT") or "3001").strip() or "3001"
    return f"http://127.0.0.1:{port}"


def _internal_token() -> str:
    return os.environ.get("BACKEND_INTERNAL_TOKEN", "")


async def publish_site(
    *,
    user_id: str,
    chat_id: str,
    src_dir: str = "",
    source_dir: str = "",
    title: str,
    slug: str = "",
    site_id: str = "",
    visibility: str = "public",
    description: str = "",
) -> Dict[str, Any]:
    if not user_id:
        return {"error": "当前会话缺少用户身份，无法发布站点"}

    payload = {
        "src_dir": src_dir,
        "source_dir": source_dir,
        "title": title,
        "slug": slug,
        "site_id": site_id,
        "visibility": visibility,
        "description": description,
        "user_id": user_id,
        "chat_id": chat_id,
    }
    headers = {"Content-Type": "application/json"}
    token = _internal_token()
    if token:
        headers["X-Internal-Token"] = token

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{_backend_url()}/v1/internal/sites/publish",
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        return {"error": f"发布请求失败（无法连到后端）: {exc}"}

    if response.status_code != 200:
        return {"error": f"发布接口返回 {response.status_code}: {response.text[:300]}"}
    try:
        envelope = response.json()
    except Exception as exc:
        return {"error": f"发布接口返回非 JSON: {exc}"}
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if isinstance(data, dict):
        return data
    return envelope if isinstance(envelope, dict) else {"error": "发布接口返回格式异常"}
