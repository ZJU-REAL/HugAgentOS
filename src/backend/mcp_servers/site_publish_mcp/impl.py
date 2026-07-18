"""site_publish MCP —— 业务实现（转发到 backend 内部发布接口）。

mcp 容器没有沙箱访问权，所以本工具不直接碰沙箱：把发布请求连同用户/会话身份
一起 POST 到 backend 的 ``/v1/internal/sites/publish``（backend 有沙箱），由它完成
"打包沙箱目录 → 取回 → 解包 → 落库托管"。鉴权走共享密钥 ``X-Internal-Token``。
"""

from __future__ import annotations

import os
from typing import Any, Dict

import httpx


def _backend_url() -> str:
    explicit = os.environ.get("BACKEND_INTERNAL_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = (os.environ.get("BACKEND_PORT") or "3001").strip() or "3001"
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
    team_id: str = "",
) -> Dict[str, Any]:
    if not user_id:
        return {"error": "当前会话缺少用户身份，无法发布站点"}
    # src_dir 可留空：项目模式下 backend 会自动定位到会话绑定的项目文件夹。
    # 非项目会话且缺少 chat_id 又没传 src_dir 时，backend 会返回可读错误。
    # source_dir（构建型站点的源码工程目录）原样透传，语义见 backend 接口。

    payload = {
        "src_dir": src_dir,
        "source_dir": source_dir,
        "title": title,
        "slug": slug,
        "site_id": site_id,
        "visibility": visibility,
        "description": description,
        "team_id": team_id,
        "user_id": user_id,
        "chat_id": chat_id,
    }
    headers = {"Content-Type": "application/json"}
    token = _internal_token()
    if token:
        headers["X-Internal-Token"] = token

    url = f"{_backend_url()}/v1/internal/sites/publish"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return {"error": f"发布请求失败（无法连到后端）: {exc}"}

    if resp.status_code != 200:
        body = ""
        try:
            body = resp.text[:300]
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"发布接口返回 {resp.status_code}: {body}"}

    try:
        envelope = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"发布接口返回非 JSON: {exc}"}

    # 后端用统一信封 {code,message,data,...}，真实结果在 data
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if isinstance(data, dict):
        return data
    return envelope if isinstance(envelope, dict) else {"error": "发布接口返回格式异常"}
