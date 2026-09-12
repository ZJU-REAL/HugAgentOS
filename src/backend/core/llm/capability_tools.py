"""Per-request connector guards; pooled clients remain account-neutral."""

import asyncio
from agentscope.tool import ToolBase, ToolChunk
from agentscope.message import TextBlock, ToolResultState
from core.capabilities import runtime
from core.capabilities.errors import CapabilityError
from core.capabilities.paths import LOCAL_PROFILE


def connector_available(run, server_id):
    runtime.validate(run)
    latest = runtime.get(run.run_id, scope_id=run.scope_id) or run
    if "mcp:" + server_id in latest.unavailable:
        return False
    binding = run.mcp_bindings.get(server_id)
    if binding is not None:
        from core.capabilities import registry

        relationships = latest.dependency_report.get("connector_parents", {})
        parents = {
            *relationships.get(server_id, []),
            *relationships.get(binding["install_id"].split(":", 2)[2], []),
        }
        for parent_id in parents:
            parent = registry.get(parent_id)
            if parent is None or not parent.enabled or parent.state == "removed":
                return False
            owner = parent.payload.get("owner_user_id")
            if owner and str(owner) != run.user_id:
                return False
    if binding is None or not binding.get("authorization_checked"):
        return True
    _, profile, key = binding["install_id"].split(":", 2)
    if profile == "local-json":
        from core.capabilities.mcp_json import local_server_configs

        return key in local_server_configs()
    if profile == LOCAL_PROFILE:
        from core.services.mcp_service import McpServerConfigService

        service = McpServerConfigService.get_instance()
        return key in {
            **service.get_all_servers(enabled_only=True),
            **service.get_owned_servers(run.user_id, enabled_only=True),
        }
    from core.services.desktop_cloud_bridge import cloud_gateway_mcp_configs

    return server_id in cloud_gateway_mcp_configs([server_id])


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
        try:
            available = await asyncio.to_thread(connector_available, self.run, self.server_id)
        except (CapabilityError, OSError):
            available = False
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
