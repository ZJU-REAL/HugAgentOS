"""Host composition for product tools until their owning extensions move in P2/P3."""


def register_product_tools():
    from core.agent_runtime.host import register as register_chat_execution
    register_chat_execution()
    from core.ontology.host import register as register_governance
    register_governance()
    from core.harness.registry.system_configuration import register_system_configuration_store
    from core.db.system_configuration import SystemConfigurationStore
    register_system_configuration_store(SystemConfigurationStore())
    from core.harness.registry.cache_invalidation import cache_invalidators
    from core.harness.registry.run_capabilities import has_run_capabilities
    from features.desktop.capabilities.local_projection import sync_local_skills
    from prompts.prompt_runtime import invalidate_prompt_cache
    cache_invalidators().register("before_skill_refresh", "desktop_skills",
                                  lambda: sync_local_skills() if has_run_capabilities() else None)
    cache_invalidators().register("after_skill_refresh", "prompt_cache", invalidate_prompt_cache)

    from core.evolution.settlement.memory_service import register as register_settlement
    register_settlement()
    from core.runs.host import register as register_runs
    register_runs()
    from core.runs.tool_effects.host import register as register_tool_effects
    register_tool_effects()
    from core.llm.host_services import register
    register()
    register_marketplace_catalog()
    register_retrieval_context()
    from importlib import import_module
    from features.installed import INSTALLED_FEATURES
    for package in INSTALLED_FEATURES:
        register = getattr(import_module(f"{package}.extension"), "register_tools", None)
        if register:
            register()
        gateways = getattr(import_module(f"{package}.extension"), "register_gateway_contributions", None)
        if gateways:
            gateways()


def register_package_sources():
    # CLI installs packages before API composition, so bind import policy here.
    from core.ontology.host import register as register_governance
    register_governance()
    register_marketplace_catalog()
    from core.harness.registry.package_views import register_package_view
    from features.workbench.ui import contract
    from features.workbench.ui import workspace_enabled
    register_package_view(
        contract.public_contributions, normalize=contract.normalize_ui, find=contract.find_module,
        enabled=workspace_enabled.enabled, workspace_only=workspace_enabled.workspace_only,
        set_enabled=workspace_enabled.set_enabled,
    )
    from core.harness.registry.package_sources import register_package_source, register_package_upgrade_policy
    from features.marketplace.package_source import MarketplacePackageSource
    from features.workbench.package_source import WorkbenchPackagePolicy
    register_package_source("marketplace", MarketplacePackageSource())
    register_package_upgrade_policy(WorkbenchPackagePolicy())


def register_resource_owners():
    from core.harness.registry.resource_owners import ResourceOwner, register_resource_owner
    from features.workbench import background, diagnostics, installation_binding, revocation_sweep
    from features.workbench import resource_owner, resource_deadlines, revocation
    from features.hosted_apps.resources import admission, cleanup
    register_resource_owner(ResourceOwner(
        binding=lambda *args, **kwargs: resource_owner.application_binding(*args, **kwargs),
        resolve_installation=lambda *args, **kwargs: resource_owner.resolve_installation(*args, **kwargs),
        exact_installation=lambda *args, **kwargs: installation_binding.exact(*args, **kwargs),
        authorize=lambda *args, **kwargs: background.authorized_resource(*args, **kwargs),
        observe=lambda *args, **kwargs: diagnostics.observe_resource(*args, **kwargs),
        record=lambda *args, **kwargs: diagnostics.record(*args, **kwargs),
        renew=lambda *args, **kwargs: background.renew(*args, **kwargs),
        arm=lambda *args, **kwargs: resource_deadlines.arm(*args, **kwargs),
        invalidate=lambda *args, **kwargs: resource_deadlines.invalidate(*args, **kwargs),
        sweep=lambda *args, **kwargs: revocation_sweep.sweep(*args, **kwargs),
        collect=lambda *args, **kwargs: resource_owner.collect(*args, **kwargs),
        create=admission.create,
        owned=admission.owned,
        lease=admission.renew,
        reserve=admission.reserve,
        close=cleanup.close,
        revoke_session=revocation.revoke_user_workbench,
    ))


def register_run_capabilities():
    from core.harness.registry.capability_revisions import register_capability_revisions
    from features.desktop.capability.revision_provider import CloudCapabilityRevisions
    register_capability_revisions(CloudCapabilityRevisions())
    from core.harness.registry.capability_management import register_manager_executor
    from features.desktop.local.manager_execution import execute
    register_manager_executor(execute)
    from core.harness.registry.device_catalog import register_device_catalog
    from features.desktop.capabilities import device_catalog
    register_device_catalog(device_catalog)
    from core.harness.registry.local_files import register_local_file_services
    from features.desktop.local import grants, snapshots
    register_local_file_services(grants, snapshots)
    from core.harness.registry.capability_runtime import register_capability_runtime
    from features.desktop.capabilities import runtime as capability_runtime
    register_capability_runtime(capability_runtime)
    from core.harness.registry.local_publication import register_local_publication
    from features.desktop.local.publication_provider import DesktopLocalPublication
    register_local_publication(DesktopLocalPublication())
    from core.harness.registry.execution_identity import register_execution_identity
    from features.desktop.bridge import identity
    register_execution_identity(identity)
    from core.harness.registry.execution_catalog import register_execution_catalog
    from core.harness.registry.execution_account import register_execution_account
    from features.desktop.bridge import gateway
    from features.desktop.capabilities import skill_account
    register_execution_catalog(gateway)
    register_execution_account(skill_account)
    from core.harness.registry.host_runtime import register_remote_execution_policy
    from features.desktop.auth import bridge as desktop_bridge
    from core.harness.registry.authentication import register_host_authentication
    register_host_authentication(desktop_bridge.claims_request, desktop_bridge.resolve_bridge_user)
    register_remote_execution_policy(lambda: desktop_bridge.bridge_enabled())
    from core.harness.registry.run_capabilities import register_run_capability_provider
    from features.desktop.capabilities import paths
    from features.desktop.run_capabilities.provider import DesktopRunCapabilityProvider
    register_run_capability_provider("desktop", DesktopRunCapabilityProvider(), enabled=lambda: paths.capabilities_enabled())


def register_model_credentials():
    from core.harness.registry.model_credentials import register_request_header_resolver
    from features.desktop.bridge import model_credentials as desktop_model_credentials
    from core.harness.registry.model_credentials import register_model_credential_provider
    register_model_credential_provider(desktop_model_credentials)
    register_request_header_resolver(
        lambda *args: desktop_model_credentials.prepare_request_headers(*args)
    )


def register_catalog_options():
    from importlib import import_module
    from features.installed import INSTALLED_FEATURES
    for package in INSTALLED_FEATURES:
        register = getattr(import_module(f"{package}.extension"), "register_catalog_options", None)
        if register:
            register()


def register_retrieval_context():
    from importlib import import_module
    from features.installed import INSTALLED_FEATURES
    for package in INSTALLED_FEATURES:
        register = getattr(import_module(f"{package}.extension"), "register_retrieval_context", None)
        if register:
            register()


def register_marketplace_catalog():
    from core.harness.registry.marketplace_catalog import register_marketplace_catalog as register_catalog
    from features.marketplace.core_catalog import MarketplaceCatalogProvider
    register_catalog(MarketplaceCatalogProvider())
