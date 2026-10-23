"""Native tool values and their domain policy declarations during assembly."""
from agentscope.tool import FunctionTool, ToolBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from core.sandbox.permissions.tool_specs import builtin_tool_permission


class ToolAssembly(list[ToolBase]):
    def __init__(self):
        super().__init__()
        self.skill_loaders = []

    @property
    def permission_specs(self):
        from extensions.mcp_servers.registry import SERVERS
        specs = {}
        for server in SERVERS.values():
            specs.update(server.permission_specs())
        for tool in self:
            spec = getattr(tool, "dispatch_spec", None) or builtin_tool_permission(tool.name)
            if spec is not None:
                previous = specs.get(tool.name)
                if previous is not None and previous.key != spec.key:
                    raise ValueError(f"Conflicting permission declaration for {tool.name!r}")
                specs[tool.name] = spec
        return specs


def function_tool(func, *, description=None, dispatch_spec=None):
    tool = FunctionTool(
        func, description=description, is_concurrency_safe=False,
        permission=PermissionDecision(behavior=PermissionBehavior.ALLOW,
                                      message="Subject to the run's execution policy"),
    )
    tool.dispatch_spec = dispatch_spec
    return tool
