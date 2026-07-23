"""Community-edition route registry."""

from importlib import import_module

from .mock_sso import login_router
from .mock_sso import router as mock_sso_router

CE_ROUTERS: tuple[tuple[str, str], ...] = (
    ("chats", "router"),
    ("auth", "router"),
    ("users", "router"),
    ("catalog", "router"),
    ("kb", "router"),
    ("summary", "router"),
    ("classify", "router"),
    ("config", "router"),
    ("file_parse", "router"),
    ("file_upload", "router"),
    ("content", "router"),
    ("memories", "router"),
    ("ontologies", "router"),
    ("models", "router"),
    ("chat_shares", "router"),
    ("agents", "router"),
    ("artifacts", "router"),
    ("plans", "router"),
    ("loops", "router"),
    ("automations", "router"),
    ("chat_runs", "router"),
    ("me_system", "router"),
    ("me_logs", "router"),
    ("myspace_folders", "router"),
    ("batch", "router"),
    ("internal_batch", "router"),
    ("internal_sites", "router"),
    ("projects", "router"),
    ("api_keys", "router"),
    ("me_capabilities", "router"),
    ("marketplace", "router"),
    ("agent_marketplace", "router"),
    ("plugins", "router"),
    ("integrations", "router"),
    ("channels", "router"),
    ("meta", "router"),
    ("sites", "router"),
    ("desktop", "router"),
)

EE_ROUTERS: tuple = ()


def iter_edition_routers(entries):
    for entry in entries:
        module_name, attr, *feature_items = entry
        module = import_module(f"{__name__}.{module_name}")
        feature = feature_items[0] if feature_items else None
        yield module_name, getattr(module, attr), feature


__all__ = [
    "CE_ROUTERS",
    "EE_ROUTERS",
    "iter_edition_routers",
    "login_router",
    "mock_sso_router",
]
