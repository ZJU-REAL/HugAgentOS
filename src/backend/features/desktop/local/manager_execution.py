"""Execute an installed manager contract after rechecking account and installation authority."""
from core.harness.registry import execution_catalog as bridge_gateway
from core.harness.registry import execution_identity as bridge_identity
from core.harness.registry import execution_account as skill_account
from core.capabilities.plugins.management.contract import declared, MANAGERS


def execute(*, manager, server_id, headers, cloud_backed, tool_name, input_schema, arguments):
    from core.harness.registry.host_runtime import requires_remote_execution as bridge_enabled
    state = None
    if cloud_backed and bridge_enabled():
        state = bridge_identity.get_state()
        bridge_identity.require_current_account(state)
        user_id = skill_account.current_local_user_id()
        context = bridge_gateway.bridge_context() or {}
        server = next((s for s in context.get("servers", []) if s["server_id"] == server_id), None)
        if not server or server.get("source_plugin") != manager:
            raise PermissionError("management plugin is no longer installed or authorized")
        # upload is a local contribution authorized by the same declared install capability.
        op = "install_skill" if tool_name == "upload_skill_to_cloud" else tool_name
        contract = next((t for t in server.get("tools", []) if t["name"] == op), None)
        if contract is None or declared(contract, manager) != manager:
            raise PermissionError("management plugin contract changed; synchronize capabilities")
    else:
        user_id = next((v for k, v in headers.items() if k.lower() == "x-current-user-id"), None)
        from core.harness.registry.execution_catalog import authorizes_manager
        if not authorizes_manager(user_id, server_id, manager):
            raise PermissionError("local management plugin is not installed")
    if not user_id:
        raise PermissionError("authentication required")
    from jsonschema import validate
    validate(arguments, input_schema)
    if state:
        bridge_identity.require_current_account(state)
    if tool_name == "upload_skill_to_cloud":
        from features.desktop.local.skills.upload import upload
        return upload(user_id, **arguments)
    kind = MANAGERS[manager]
    from features.desktop.local.skills import service as local_skill_service
    from features.desktop.local.plugins import service as local_plugin_service
    service = local_skill_service if kind == "skill" else local_plugin_service
    action = tool_name.removesuffix("_" + kind)
    if tool_name == "list_" + kind + "s":
        return {"ok": True, "items": getattr(service, "list_" + kind + "s")(user_id)}
    args = dict(arguments)
    if "source" in args:
        source = args.pop("source")
        if source.get("kind") != "local_path":
            raise ValueError("local installation requires source.kind=local_path")
        args["source_path"] = source["path"]
    return getattr(service, action)(user_id, **args)
