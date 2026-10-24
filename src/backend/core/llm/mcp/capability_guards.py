"""Per-request connector guards; pooled clients remain account-neutral."""

import asyncio
from agentscope.tool import ToolBase, ToolChunk
from agentscope.message import TextBlock, ToolResultState


def connector_available(run, server_id):
    return run.validate_tool_call(server_id)


class AvailableMCPTool(ToolBase):
    def __init__(self, tool, run, server_id):
        self.tool, self.run, self.server_id = tool, run, server_id
        for name in (
            "name",
            "description",
            "input_schema",
            "is_concurrency_safe",
            "is_read_only",
            "is_external_tool",
            "is_state_injected",
            "is_mcp",
            "mcp_name",
        ):
            setattr(self, name, getattr(tool, name))

    def __getattr__(self, name):
        return getattr(self.tool, name)

    async def check_permissions(self, *args, **kwargs):
        return await self.tool.check_permissions(*args, **kwargs)

    async def __call__(self, **kwargs):
        available = await asyncio.to_thread(connector_available, self.run, self.server_id)
        if not available:
            return ToolChunk(
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        text="该连接器已停用或当前不可用，本次调用未执行。请说明情况并继续处理可完成的部分。"
                    )
                ],
            )
        # Do not catch an uncertain write outcome or retry a side effect.
        return await self.tool(**kwargs)


class AvailableMCPClient:
    def __init__(self, client, run):
        self.client, self.run = client, run
        self.name = client.name

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def list_tools(self):
        return [
            AvailableMCPTool(tool, self.run, self.name) for tool in await self.client.list_tools()
        ]

    async def get_tool(self, name):
        return AvailableMCPTool(await self.client.get_tool(name), self.run, self.name)
