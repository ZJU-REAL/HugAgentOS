"""Build native tools from an authorized cloud or local management manifest."""
from core.capabilities.mcp.tool_schema import has_usable_schema
from core.capabilities.plugins.management.contract import declared, localize, tools as manager_tools
from core.harness.registry.run_capabilities import has_run_capabilities
from core.llm.tools.cloud_gateway import GatewayMCPTool
from core.llm.tools.management import ManagementTool


def manifest_tools(name, cfg):
    plugin = str(cfg.get("gateway_plugin") or "")
    url = str(cfg.get("gateway_invoke_url") or "")
    schema_hash = str(cfg.get("schema_hash") or "")
    items = cfg.get("manifest_tools")
    if not isinstance(items, list):
        raise ValueError("Missing authorized tool manifest")
    if cfg.get("schema_source") == "cloud_manifest" and not (url and schema_hash):
        raise ValueError("Cloud manifest requires an invocation URL and schema hash")
    if cfg.get("schema_source") == "plugin_manifest" and not (
        cfg.get("origin") == "local_plugin" and all(declared(t, plugin) for t in items)
    ):
        raise ValueError("Invalid local management manifest")
    if not all(isinstance(t, dict) for t in items):
        raise ValueError("Invalid manifest tool entry")
    items = list(items)
    from extensions.mcp_servers.registry import gateway_server
    if cfg.get("read_only") and gateway_server(name) is not None:
        items = [t for t in items if t.get("annotations", {}).get("readOnlyHint") is True]
    local = has_run_capabilities()
    if local and plugin == "skill-manager" and any(t.get("name") == "install_skill" and declared(t, plugin) for t in items):
        items += [t for t in manager_tools(plugin, "local") if t["name"] == "upload_skill_to_cloud" and not any(x.get("name") == t["name"] for x in items)]
    result = []
    names = set()
    for item in items:
        if local:
            item = localize(item, plugin, cloud_available=bool(cfg.get("manager_cloud_available") or url))
        if item is None or not has_usable_schema(item):
            continue
        tool_name = item.get("name")
        if not isinstance(tool_name, str) or not tool_name or tool_name in names:
            raise ValueError("Invalid or duplicate manifest tool name")
        names.add(tool_name)
        if cfg.get("enable_tools") is not None and tool_name not in cfg["enable_tools"]:
            continue
        if tool_name in (cfg.get("disable_tools") or []):
            continue
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(item["inputSchema"])
        headers = dict(cfg.get("headers") or {})
        if local and declared(item, plugin):
            result.append(ManagementTool(name, item, plugin, headers, cloud_backed=bool(url)))
        else:
            result.append(GatewayMCPTool(mcp_name=name, tool=item, invoke_url=url,
                schema_hash=schema_hash, headers=headers, timeout=cfg.get("execution_timeout") or 120,
                transport=cfg.get("gateway_transport"), source_plugin=plugin))
    return result
