"""Local execution of an explicitly declared, installed manager plugin contract."""
import asyncio
import json
from agentscope.tool import ToolBase, ToolChunk
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from core.capabilities.plugins.management.errors import management_error


class ManagementTool(ToolBase):
    is_mcp = True
    is_state_injected = False
    is_external_tool = False
    is_concurrency_safe = False

    def __init__(self, server_id, tool, manager, headers, *, cloud_backed):
        super().__init__()
        self.mcp_name, self.name = server_id, tool["name"]
        self.description, self.input_schema = tool.get("description", ""), tool["inputSchema"]
        self.manager, self.headers = manager, dict(headers)
        self.cloud_backed = cloud_backed
        self.is_read_only = self.name.startswith(("get_", "list_"))

    async def check_permissions(self, *_args, **_kwargs):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW if self.is_read_only else PermissionBehavior.ASK,
                                  message="管理当前用户的本机技能或插件")

    def _execute(self, arguments):
        from core.harness.registry.capability_management import execute_manager
        return execute_manager(
            manager=self.manager, server_id=self.mcp_name, headers=self.headers,
            cloud_backed=self.cloud_backed, tool_name=self.name,
            input_schema=self.input_schema, arguments=arguments,
        )

    async def call(self, **kwargs):
        try:
            result = await asyncio.to_thread(self._execute, kwargs)
            return ToolChunk(content=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
                             state=ToolResultState.SUCCESS,
                             metadata={"origin": "local", "capabilities_changed": not self.is_read_only})
        except Exception as exc:
            message = management_error(exc)
            return ToolChunk(content=[TextBlock(text=json.dumps({"ok": False, "error": message}, ensure_ascii=False))],
                             state=ToolResultState.ERROR, metadata={"origin": "local"})
