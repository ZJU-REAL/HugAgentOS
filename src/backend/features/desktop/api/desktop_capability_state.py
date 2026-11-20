"""Shared state of the desktop capability gateway routes: lazy upstream HTTP client, bearer scheme and header forwarding rules."""
from __future__ import annotations
from typing import Optional
import httpx
from fastapi.security import HTTPBearer




gateway_client: Optional[httpx.AsyncClient] = None


bearer = HTTPBearer(auto_error=False)


DROP_REQUEST_HEADERS = {
    # 身份与凭据：capability token 不上传上游；身份头由网关重写
    "authorization",
    "cookie",
    "x-current-user-id",
    # 知识库授权按云端用户重算（头缺失时 KB MCP 自动解析可用知识库）
    "x-allowed-kb-ids",
    # 桌面桥内部头
    "x-desktop-bridge",
    "x-desktop-bridge-user",
    "x-desktop-device-id",
    "x-hugagent-target",
    # 逐跳 / 传输层头，由 httpx 按新连接重建
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "transfer-encoding",
    "keep-alive",
    "proxy-authorization",
    "te",
    "upgrade",
}


FWD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version")


RUNTIME_CONTEXT_HEADERS = {
    "x-chat-id",
    "x-channel-id",
    "x-conversation-id",
    "x-allowed-dataset-ids",
    "x-reranker-enabled",
}
