"""Host composition for installed external-service integrations."""
def register_credentials():
    from importlib import import_module
    from features.installed import INSTALLED_FEATURES
    for package in INSTALLED_FEATURES:
        register = getattr(import_module(f"{package}.extension"), "register_credentials", None)
        if register:
            register()
