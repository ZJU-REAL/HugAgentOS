"""Feature module registration template — features/${feature}/extension.py.

Replace ${Feature}, ${feature} with actual names.

Contract (core/harness/registry/__init__.py::load_extensions, features/__init__.py):
  - NAME: str                      module name used in logs and is_enabled()
  - setup() -> bool                called once at startup; register hooks/routers/workers and return True,
                                   or register nothing and return False when switched off (env flag etc.)
  - Optional hooks the host looks up by name:
      register_tools()                    agent tool contributions (product tool composition)
      register_gateway_contributions()    desktop gateway upload/receipt channels
      register_catalog_options()          capability catalog options
      register_retrieval_context()        retrieval context providers
  - models.py is imported for every INSTALLED_FEATURES entry regardless of setup() — schema registration
    must not depend on runtime switches

Core never imports a feature's implementation: features call core.harness.registry.* to plug in.
"""

import os

NAME = "${feature}"


def enabled() -> bool:
    return os.getenv("${FEATURE}_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def setup() -> bool:
    if not enabled():
        return False
    # Routes that must disappear when the module is off are mounted here instead of CE_ROUTERS.
    # Always-on routes go into api/routes/v1/__init__.py::CE_ROUTERS (ordering is explicit there).
    from core.harness.registry import register_router
    from features.${feature}.api.${feature}s import router

    register_router(router)  # optional policy_key="content_admin" etc. applies the EE edition gate
    return True
