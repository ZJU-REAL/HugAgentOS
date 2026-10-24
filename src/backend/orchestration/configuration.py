"""Install database-backed configuration before loading application services."""

def register_configuration():
    from core.db.system_configuration import SystemConfigurationStore
    from core.harness.registry.system_configuration import register_system_configuration_store
    register_system_configuration_store(SystemConfigurationStore())
