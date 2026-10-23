"""Developer-owned execution policy for the space MCP contract."""

from core.sandbox.permissions.dispatch_models import DOMAIN_APPROVAL, DOMAIN_DENY, WRITE, PermissionIntent, ToolPermissionSpec

from .server import ARGUMENTS, READ_TOOL, WRITE_TOOL


def permission_specs():
    def resolve_read(arguments, runtime):
        ARGUMENTS[READ_TOOL].model_validate(arguments)
        return ()

    def resolve_write(arguments, runtime):
        if runtime.read_only:
            return (PermissionIntent(DOMAIN_DENY, "deny", "", "只读会话不能修改云端空间文件"),)
        args = ARGUMENTS[WRITE_TOOL].model_validate(arguments)
        target = args.space + "/" + args.path
        labels = {"write": "写入", "mkdir": "创建目录", "move": "移动", "delete": "删除"}
        summary = labels[args.operation] + "云端空间文件 " + target
        if args.operation == "move":
            summary += " → " + args.destination
        return (
            PermissionIntent(
                domain=DOMAIN_APPROVAL,
                action=WRITE,
                target=target,
                summary=summary,
                op=args.operation,
                kind="tool_permission",
            ),
        )

    return {
        READ_TOOL: ToolPermissionSpec("mcp:space-files:read", resolve_read),
        WRITE_TOOL: ToolPermissionSpec("mcp:space-files:write", resolve_write),
    }
