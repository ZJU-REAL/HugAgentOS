"""Plugin browser tools forward trusted invocation context to the owning backend."""
import os
import uuid
from typing import Any
import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp_servers._ports import PORTS
from mcp_servers._serve import run

mcp = FastMCP("browser-automation")

async def call(ctx, action, resource_id=None, params=None, checkpoint_id=None):
    headers = ctx.request_context.request.headers
    from core.llm.mcp_invocation import verify
    verify(headers, "browser_runtime")
    user_id = headers.get("x-current-user-id", "")
    chat_id = headers.get("x-chat-id") or headers.get("x-conversation-id", "")
    if not user_id or not chat_id:
        raise ValueError("conversation_identity_required")
    base = os.getenv("BACKEND_INTERNAL_URL") or ("http://127.0.0.1:" + (os.getenv("BACKEND_PORT") or os.getenv("PORT") or "3001"))
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        response = await client.post(base.rstrip("/") + "/v1/internal/plugin-resources/tool", json={
            "slug": "browser-automation", "module_id": "browser", "chat_id": chat_id,
            "resource_id": resource_id, "action": action, "params": params or {},
            "checkpoint_id": checkpoint_id, "command_id": uuid.uuid4().hex,
        }, headers={"X-Internal-Token": os.getenv("BACKEND_INTERNAL_TOKEN", ""), "X-Current-User-Id": user_id,
                    "X-Chat-Id": chat_id, "X-Hugagent-Mcp-Audience": "browser_runtime",
                    "X-Hugagent-Invocation": headers.get("X-Hugagent-Invocation", "")})
        if response.status_code >= 400:
            raise ValueError(response.json().get("detail", "browser_runtime_unavailable"))
        return response.json()["data"]

@mcp.tool()
async def browser_open(ctx: Context, resource_id: str | None = None, checkpoint_id: str | None = None) -> dict:
    """Create a browser in this conversation, or resume an exact session. Opens its live Canvas. Reuse the returned resource_id in every later call. Optional checkpoint_id restores a login explicitly saved by the user."""
    return await call(ctx, "open", resource_id, checkpoint_id=checkpoint_id)

@mcp.tool()
async def browser_observe(ctx: Context, resource_id: str, action: str = "snapshot", selector: str = "body") -> dict:
    """Observe the shared browser: snapshot, text, screenshot or state. Observation pauses during manual/private control. Snapshot before selecting a locator."""
    if action not in {"snapshot", "text", "screenshot", "state"}:
        raise ValueError("unsupported_observation")
    return await call(ctx, action, resource_id, {"selector": selector})

@mcp.tool()
async def browser_action(ctx: Context, resource_id: str, action: str, params: dict[str, Any] | None = None) -> dict:
    """Operate the shared browser. Actions: navigate(url), click(selector or role/name), fill(text,selector or role/name), select(value,selector), key(key), back, forward, reload, new_tab(url), select_tab(tab_id), close_tab(tab_id), dialog(accept,text), upload(files), resize(width,height), retain_download(download_id) to persist a download as a conversation artifact before closing. Use observed locators; stop when control is held by the user. Never repeat a submitted action after a connection error without inspecting the page."""
    if action not in {"navigate", "click", "fill", "select", "key", "back", "forward", "reload", "new_tab", "select_tab", "close_tab", "dialog", "upload", "resize", "retain_download"}:
        raise ValueError("unsupported_action")
    return await call(ctx, action, resource_id, params)

@mcp.tool()
async def browser_close(ctx: Context, resource_id: str) -> dict:
    """Explicitly close the browser process and invalidate its viewing tickets."""
    return await call(ctx, "close", resource_id)

if __name__ == "__main__":
    run(mcp, PORTS["browser_runtime"])
