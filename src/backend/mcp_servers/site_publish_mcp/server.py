#!/usr/bin/env python3
"""Community-edition site publishing MCP server."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from core.services.application_schema import ApplicationPayload, ToolDefinition
from pydantic import BaseModel

from mcp.server.fastmcp import Context, FastMCP
from mcp_servers.site_publish_mcp import impl

mcp = FastMCP("hugagent-site-publish")

_HDR_USER = "x-current-user-id"
_HDR_CHAT = "x-chat-id"
_HDR_CONV = "x-conversation-id"


def _hdr(ctx: Optional[Context], name: str) -> Optional[str]:
    if ctx is None:
        return None
    try:
        value = ctx.request_context.request.headers.get(name)
        return value or None
    except Exception:
        return None


def _identity(ctx: Optional[Context]) -> Dict[str, str]:
    """Identity/session come from the runtime headers agent_factory injects."""
    return {
        "user_id": _hdr(ctx, _HDR_USER) or "",
        "chat_id": _hdr(ctx, _HDR_CHAT) or _hdr(ctx, _HDR_CONV) or "",
    }


@mcp.tool()
async def list_sites(ctx: Context | None = None) -> Dict[str, Any]:
    """List the account's published sites the caller may edit, with their source folders.

    Call this before changing any published site — from a site card, the site's
    project chat, or a brand-new conversation. The returned ``site_id`` is what
    ``publish_site`` needs to update the original site; publishing without it
    creates a new site and leaves the user's existing URL untouched.

    Each item carries ``site_id``, ``title``, ``url``, ``version``, ``kind``
    (``static`` or ``build``), ``source_dir`` (where to edit), ``publish_dir``
    (``src_dir`` for static sites; empty for build sites, which must be rebuilt),
    ``in_current_project`` and ``editable``.

    With one candidate, use it. With several and no clear request, ask the user —
    never default to the most recently published one. An error or an empty list
    does not mean the user has no sites, so never create a new site because of it.
    """
    return await impl.list_sites(**_identity(ctx))


@mcp.tool()
async def publish_site(
    title: str,
    src_dir: str = "",
    source_dir: str = "",
    slug: str = "",
    site_id: str = "",
    visibility: str = "public",
    description: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Publish a site from the current sandbox and return its hosted URL.

    ``visibility`` accepts ``public`` or ``private``. Static sites may omit
    ``src_dir`` in a project chat. Build-based sites pass the build output as
    ``src_dir`` and the editable source folder as ``source_dir``.

    To update a published site, call ``list_sites`` first and pass that
    ``site_id`` back here; omitting it creates a separate new site.
    """
    return await impl.publish_site(
        **_identity(ctx),
        src_dir=src_dir,
        source_dir=source_dir,
        title=title,
        slug=slug,
        site_id=site_id,
        visibility=visibility,
        description=description,
    )


@mcp.tool()
async def manage_application(
    action: Literal[
        "list",
        "create",
        "table",
        "insert",
        "query",
        "publish_mcp",
        "revoke_mcp",
        "source",
        "publish_project",
    ],
    payload: ApplicationPayload | None = None,
    app_id: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """管理应用数据库、记录和 MCP 项目。

    action 指定操作，app_id 放在顶层，payload 按工具 schema 提供。
    list 查询已有应用；create 使用用户确定的标题，可选关联 site_id。
    kind=mcp 创建 MCP 源码项目；kind=data 创建数据应用。
    table 定义表与字段，字段类型为 text、integer、number、boolean、date、json。
    insert 写入用户提供或授权来源的记录；query 按字段过滤并分页查询。
    数据来源、表名、字段和查询范围由用户需求及实际数据决定。
    source 返回项目草稿和已发布定义；publish_project 发布项目中的 mcp.json，
    校验应用编号与版本，更新原服务并回写项目版本。
    publish_mcp 发布显式工具定义；revoke_mcp 停用服务及个人连接。
    独立 publish_mcp 工具用于发布并接入当前用户的个人 MCP。
    访问权限须显式配置，凭据由服务端保存，不写入源码或返回对话。
    """
    identity = _identity(ctx)
    if not identity["user_id"]:
        return {"error": "Current user identity is required"}
    return await impl._call_backend(
        "/v1/internal/applications/operation",
        {
            **identity,
            "action": action,
            "payload": payload.model_dump() if isinstance(payload, BaseModel) else (payload or {}),
            "app_id": app_id,
        },
        timeout=60.0,
    )


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9113)


@mcp.tool()
async def publish_mcp(
    app_id: str,
    tools: list[ToolDefinition],
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """发布已有应用的 MCP，并验证连接后添加到当前用户的个人 MCP。

    从已有应用的数据表定义查询工具；应用编号、工具名称和查询范围取自实际需求。
    工具只暴露批准的表、字段和过滤条件。重复发布更新同一个个人 MCP。
    当前身份由平台注入；不接受自定义 URL、账号或凭据。需要个人 MCP 创建权限。
    成功回执含 app_id、server_id、地址、版本、installed 和 connection_verified；
    只有两项都为 true 才能宣称接入成功。部分失败保留发布结果，修复后重试。
    访问凭据加密保存在服务端，不返回对话，不写入页面或源码。
    手动对外接入需在站点管理中发布并取得单独的访问凭据。
    """
    identity = _identity(ctx)
    if not identity["user_id"]:
        return {"error": "Current user identity is required"}
    return await impl._call_backend(
        "/v1/internal/applications/publish-mcp",
        {**identity, "app_id": app_id, "tools": [tool.model_dump() for tool in tools]},
        timeout=60.0,
    )


if __name__ == "__main__":
    main()
