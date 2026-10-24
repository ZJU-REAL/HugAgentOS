"""Desktop implementation of the kernel's per-run capability contracts."""


class DesktopRunCapabilityProvider:
    def cloud_plugin_selection(self, plugin_id, user_id):
        from features.desktop.capabilities.invocation import cloud_plugin_selection
        return cloud_plugin_selection(plugin_id, user_id=user_id)

    def resolve_explicit_ids(self, user_id, skill_ids, mcp_ids):
        from features.desktop.capabilities.invocation import resolve_explicit_ids
        return resolve_explicit_ids(user_id, skill_ids, mcp_ids)

    def plugin_skill_defaults(self, user_id, skill_ids):
        from features.desktop.plugin_resolution import prepare_desktop_plugin_skill_defaults
        return prepare_desktop_plugin_skill_defaults(user_id, skill_ids)

    def ensure_ready(self, user_id, *, skill_keys=None, plugin_keys=None):
        from features.desktop.capabilities.preparation import ensure_cloud_ready
        return ensure_cloud_ready(user_id, skill_keys=skill_keys or (), plugin_keys=plugin_keys or ())

    def available_skill_names(self, names, *, user_id):
        from features.desktop.capabilities.skills import filter_available_names
        return filter_available_names(names, user_id=user_id)

    def plugin_binding_ids(self, plugin_ids, *, user_id):
        from features.desktop.capabilities.plugins import cloud_binding_ids
        return cloud_binding_ids(plugin_ids, user_id=user_id)

    async def prepare(self, request, **selection):
        from features.desktop.run_capabilities.preparation import device_preparation
        return await device_preparation(request, **selection)


class PreparedCapabilityOperations:
    def skill_loader(self):
        from features.desktop.capabilities.runtime import frozen_loader
        return frozen_loader(self)

    def bind_mcp(self, servers, resolution):
        from features.desktop.capabilities.runtime import bind_mcp
        return bind_mcp(self, servers, resolution)

    def preflight(self, **context):
        from features.desktop.capabilities.runtime import preflight
        return preflight(self, **context)

    def tool_guard(self, *, scope_id):
        from features.desktop.capabilities.runtime import get
        run = get(self.run_id, scope_id=scope_id) or self
        return run.validate_tool_call

    def validate_selected_skills(self, skill_ids):
        from core.harness.capability_errors import PackageMissing
        missing = set(skill_ids or []) - set(self.bindings)
        if missing:
            raise PackageMissing("selected skills are absent from this prepared run", details={"skills": sorted(missing)})

    def validate(self, *, user_id):
        from features.desktop.capabilities.runtime import validate
        return validate(self, user_id=user_id)

    def record_tool_scope(self, tool_call_id, tool_name):
        from features.desktop.capabilities.runtime import record_tool_scope
        return record_tool_scope(self, tool_call_id, tool_name)

    def child_scope(self, kind, *identities):
        from core.harness.registry.capability_runtime import child_scope
        return child_scope(self.scope_id, kind, *identities)

    def validate_tool_call(self, server_id):
        from features.desktop.capabilities import runtime
        from core.harness.capability_errors import CapabilityError
        from core.capabilities.identity_rules import LOCAL_PROFILE
        try:
            runtime.validate(self)
            latest = runtime.get(self.run_id, scope_id=self.scope_id) or self
            if "mcp:" + server_id in latest.unavailable:
                return False
            binding = self.mcp_bindings.get(server_id)
            if binding is not None:
                from features.desktop.capabilities import registry

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
                    if owner and str(owner) != self.user_id:
                        return False
            if binding is None or not binding.get("authorization_checked"):
                return True
            _, profile, key = binding["install_id"].split(":", 2)
            if profile == "local-json":
                from features.desktop.capabilities.mcp_json import local_server_configs

                return key in local_server_configs()
            if profile == LOCAL_PROFILE:
                from core.capabilities.mcp.service import McpServerConfigService

                service = McpServerConfigService.get_instance()
                return key in {
                    **service.get_all_servers(enabled_only=True),
                    **service.get_owned_servers(self.user_id, enabled_only=True),
                }
            from features.desktop.bridge.gateway import cloud_gateway_mcp_configs

            return server_id in cloud_gateway_mcp_configs([server_id])
        except (CapabilityError, OSError):
            return False

    def release(self):
        """Run snapshots remain durable with history under the existing retention policy."""
        return None
