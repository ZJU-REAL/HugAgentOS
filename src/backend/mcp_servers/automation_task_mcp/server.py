#!/usr/bin/env python3
"""streamable-http MCP server：定时任务管理（创建/查看/修改/暂停/恢复/删除）。

用户身份与渠道上下文经 HTTP 头注入（由后端 agent_factory 设置）：
    X-Current-User-Id     当前用户（所有操作按它归属，缺失则拒绝）
    X-Channel-Id          当前渠道会话的 channel_id（渠道 run 才有，用于 deliver_to="here"）
    X-Conversation-Id     当前渠道会话的 conversation_id

工具直连后端 DB（复用 AutomationService），不跨用户。投递目标见 delivery_targets 模型。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP

from mcp_servers.automation_task_mcp import impl

mcp = FastMCP("hugagent-automation-task")

_HDR_USER = "x-current-user-id"
_HDR_CHANNEL = "x-channel-id"
_HDR_CONV = "x-conversation-id"


def _hdr(ctx: Optional[Context], name: str) -> Optional[str]:
    if ctx is None:
        return None
    try:
        v = ctx.request_context.request.headers.get(name)
        return v or None
    except Exception:
        return None


def _user(ctx: Optional[Context]) -> str:
    return _hdr(ctx, _HDR_USER) or ""


def _channel_origin(ctx: Optional[Context]) -> Dict[str, Any]:
    cid = _hdr(ctx, _HDR_CHANNEL)
    conv = _hdr(ctx, _HDR_CONV)
    return {"channel_id": cid, "conversation_id": conv} if (cid and conv) else {}


@mcp.tool()
async def create_scheduled_task(
    cron_expression: str,
    prompt: str,
    name: str = "",
    deliver_to: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """创建定时/周期任务：到点自动执行 prompt，结果按 deliver_to 投递。

    【何时必须调用】用户想"按时间表反复或定点做某事"就调用，例如：
    "每天9点发日报"、"每周一汇总数据"、"每隔1小时提醒我"、"每月1号发账单"、
    以及任何含"定时/周期/每天/每周/每隔/提醒我/到点/cron"的请求。
    【铁律】这是创建定时任务的唯一方式。未成功调用本工具拿到 ✅ 前，禁止声称"已创建/已设置"。

    参数：
    - cron_expression：5 段 cron「分 时 日 月 周」，Asia/Shanghai 时区。
        每天9点="0 9 * * *"；每周一9点="0 9 * * 1"；每小时="0 * * * *"；每5分钟="*/5 * * * *"。
    - prompt：到点要执行的**完整自包含**指令（执行时无对话上下文），写清做什么、产出什么。
    - name：任务名称（可选，便于识别）。
    - deliver_to：结果投递到哪，**一般留空**。
        · 留空 → 自动：在飞书等渠道会话里创建就推回**该会话**；在网页里创建就生成一条**站内侧栏会话**。
        · "inapp" → 强制只发**站内/页面端**（即便在渠道里，也不推回群/私聊）。
        · 某个 conversation_id → 投到**指定的另一个渠道会话**（如"往运营群发"）。要先调
          list_channel_conversations 拿到目标会话的 conversation_id，不要凭空臆造。
    """
    return impl.create_task(
        user_id=_user(ctx), cron_expression=cron_expression, prompt=prompt,
        name=name, deliver_to=deliver_to, channel_origin=_channel_origin(ctx),
    )


@mcp.tool()
async def list_channel_conversations(ctx: Context | None = None) -> Dict[str, Any]:
    """列出我的渠道机器人（飞书等）产生过的会话（群/私聊）及其 conversation_id。

    当用户要把定时任务结果**发到"别的"会话/群**（不是当前会话）时，先调本工具拿到目标会话的
    conversation_id，再把它作为 create_scheduled_task 的 deliver_to。按用户说的群名/对象匹配 title。
    """
    return impl.list_conversations(user_id=_user(ctx))


@mcp.tool()
async def list_scheduled_tasks(
    status: str = "active",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """列出我的定时任务（任务ID/名称/cron/下次执行/状态/投递目标）。

    用户问"我有哪些定时任务/查看我的定时任务/有哪些计划任务"时调用。
    参数 status："active"(默认,仅生效中) / "paused" / "all"(全部)。
    """
    return impl.list_tasks(user_id=_user(ctx), status=status)


@mcp.tool()
async def get_scheduled_task(
    task_ref: str,
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """查看某个定时任务的详情 + 最近几次运行记录。

    task_ref 可传 task_id 或任务名称（名称模糊匹配；匹配到多个会返回候选让你向用户确认）。
    """
    return impl.get_task(user_id=_user(ctx), task_ref=task_ref)


@mcp.tool()
async def update_scheduled_task(
    task_ref: str,
    cron_expression: str = "",
    prompt: str = "",
    name: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """修改定时任务：可改执行时间(cron)、执行内容(prompt)、名称（只传要改的）。

    用户说"把每日日报改成每周一/改下午6点/改成…内容"时调用。
    task_ref 传 task_id 或名称（多命中会要求澄清）。
    """
    return impl.update_task(
        user_id=_user(ctx), task_ref=task_ref,
        cron_expression=cron_expression or None,
        prompt=prompt or None, name=name or None,
    )


@mcp.tool()
async def pause_scheduled_task(task_ref: str, ctx: Context | None = None) -> Dict[str, Any]:
    """暂停一个定时任务（暂停后到点不再触发，可随后恢复）。task_ref=task_id 或名称。"""
    return impl.pause_task(user_id=_user(ctx), task_ref=task_ref)


@mcp.tool()
async def resume_scheduled_task(task_ref: str, ctx: Context | None = None) -> Dict[str, Any]:
    """恢复一个被暂停的定时任务。task_ref=task_id 或名称。"""
    return impl.resume_task(user_id=_user(ctx), task_ref=task_ref)


@mcp.tool()
async def delete_scheduled_task(task_ref: str, ctx: Context | None = None) -> Dict[str, Any]:
    """删除/取消一个定时任务（不可恢复）。task_ref=task_id 或名称。

    【铁律】未成功调用本工具拿到 ✅ 前不要声称已删除。匹配到多个任务时必须先向用户确认，禁止猜删。
    """
    return impl.delete_task(user_id=_user(ctx), task_ref=task_ref)


def main() -> None:
    from mcp_servers import _serve
    _serve.run(mcp, default_port=9108)


if __name__ == "__main__":
    main()
