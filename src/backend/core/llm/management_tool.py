"""Local execution of an explicitly declared, installed manager plugin contract."""
import asyncio
import json
from agentscope.tool import ToolBase, ToolChunk
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from core.services.management_contract import declared, MANAGERS


class ManagementTool(ToolBase):
    is_mcp = True
    is_state_injected = False
    is_external_tool = False
    is_concurrency_safe = False

    def __init__(self, server_id, tool, manager, headers, *, cloud_backed):
        self.mcp_name, self.name = server_id, tool.name
        self.description, self.input_schema = tool.description, tool.inputSchema
        self.manager, self.headers = manager, dict(headers)
        self.cloud_backed = cloud_backed
        self.is_read_only = self.name.startswith(("get_", "list_"))

    async def check_permissions(self, *_args, **_kwargs):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW if self.is_read_only else PermissionBehavior.ASK,
                                  message="管理当前用户的本机技能或插件")

    def _execute(self, arguments):
        from core.services import desktop_cloud_bridge as bridge
        from core.capabilities import skills
        from core.auth.desktop_bridge import bridge_enabled
        state = None
        if self.cloud_backed and bridge_enabled():
            state = bridge.get_state()
            bridge.require_current_account(state)
            user_id = skills.current_local_user_id()
            context = bridge._bridge_context() or {}
            server = next((s for s in context.get("servers", []) if s["server_id"] == self.mcp_name), None)
            if not server or server.get("source_plugin") != self.manager:
                raise PermissionError("management plugin is no longer installed or authorized")
            # upload is a local contribution authorized by the same declared install capability.
            op = "install_skill" if self.name == "upload_skill_to_cloud" else self.name
            contract = next((t for t in server.get("tools", []) if t["name"] == op), None)
            if contract is None or declared(contract, self.manager) != self.manager:
                raise PermissionError("management plugin contract changed; synchronize capabilities")
        else:
            user_id = next((v for k, v in self.headers.items() if k.lower() == "x-current-user-id"), None)
            from core.capabilities.local_plugin_runtime import authorizes_manager
            if not authorizes_manager(user_id, self.mcp_name, self.manager):
                raise PermissionError("local management plugin is not installed")
        if not user_id:
            raise PermissionError("authentication required")
        from jsonschema import validate
        validate(arguments, self.input_schema)
        if state:
            bridge.require_current_account(state)
        if self.name == "upload_skill_to_cloud":
            from core.services.local_skill_upload import upload
            return upload(user_id, **arguments)
        kind = MANAGERS[self.manager]
        from core.services import local_skill_service, local_plugin_service
        service = local_skill_service if kind == "skill" else local_plugin_service
        action = self.name.removesuffix("_" + kind)
        if self.name == "list_" + kind + "s":
            return {"ok": True, "items": getattr(service, "list_" + kind + "s")(user_id)}
        args = dict(arguments)
        if "source" in args:
            source = args.pop("source")
            if source.get("kind") != "local_path":
                raise ValueError("local installation requires source.kind=local_path")
            args["source_path"] = source["path"]
        return getattr(service, action)(user_id, **args)

    async def __call__(self, **kwargs):
        try:
            result = await asyncio.to_thread(self._execute, kwargs)
            return ToolChunk(content=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
                             state=ToolResultState.SUCCESS,
                             metadata={"origin": "local", "capabilities_changed": not self.is_read_only})
        except Exception as exc:
            message = str(getattr(exc, "detail", exc))
            return ToolChunk(content=[TextBlock(text=json.dumps({"ok": False, "error": message}, ensure_ascii=False))],
                             state=ToolResultState.ERROR, metadata={"origin": "local"})
