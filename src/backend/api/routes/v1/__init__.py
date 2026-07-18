"""API v1 routes — Edition router registry (CE/EE split seam C1).

This file is the **single source of truth shared by both the CE and EE trees**:
`api.app` registers routers from these tables, CE first then EE.
Entries are ``(module name, router attribute)`` or
``(module name, router attribute, license feature bit)``; an EE entry whose
feature bit is None is **explicitly exempt** from the feature guard. When a
module is missing (the CE derived tree physically deletes EE files),
``iter_edition_routers`` silently skips it — so this file goes into the CE
tree as-is, no overlay copy needed.

Route modules no longer do named eager re-exports (no consumers repo-wide);
when a single module is needed, import it directly via
``from api.routes.v1 import chats``.
Table order preserves the historical include order (relative order within a
prefix family is immutable: the public config read must come before the
config_* admin consoles).
"""

from .mock_sso import router as mock_sso_router, login_router

CE_ROUTERS: tuple[tuple[str, str], ...] = (
    ("chats", "router"),
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
    ("models", "router"),
    ("chat_shares", "router"),
    ("agents", "router"),
    ("artifacts", "router"),
    ("plans", "router"),
    ("loops", "router"),
    ("automations", "router"),
    ("chat_runs", "router"),
    ("me", "router"),
    # Personal system settings / personal logs (CE hand-down: model access goes
    # through the models.py gate swap; service configs and the user's own call
    # logs go through these two routes. EE registers them too, but the frontend
    # only shows the entry points on CE.)
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
    ("lab_skill_distill", "router"),
    ("meta", "router"),
    ("sites", "router"),
    ("desktop", "router"),
)

# EE routers + license feature bits (M4 second line of defense; the first is
# that the CE tree physically does not contain these files).
# The three entries with feature bit None are explicit exemptions: config_verify
# is the console login check, config_license is the entry point for swapping the
# license, and auth is login/session infrastructure (session/check, logout,
# ticket exchange for local mock login) — these must stay reachable even when
# the license is invalid, otherwise users get stuck in a
# "402 → logout → login → 402" loop with no way to replace the license.
# SSO-specific features guard themselves: authorize-url attaches
# requires_feature on the auth route; the remote ticket exchange is checked
# inside the remote branch of core/auth/sso.exchange_ticket (mock/remote fork
# there — a route-level check keyed on login_mode would be bypassed by legacy
# configs).
EE_ROUTERS: tuple[tuple[str, str, str | None], ...] = (
    ("audit", "router", "audit"),
    ("admin_skills", "router", "content_admin"),
    ("admin_kb", "router", "content_admin"),
    ("admin_prompts", "router", "content_admin"),
    ("admin_mcp_servers", "router", "content_admin"),
    ("admin_agents", "router", "content_admin"),
    ("config_verify", "router", None),
    ("admin_usage_logs", "router", "billing"),
    ("admin_billing", "router", "billing"),
    ("admin_chat_history", "router", "audit"),
    ("auth", "router", None),
    ("admin_logs", "router", "audit"),
    ("admin_skill_drafts", "router", "content_admin"),
    ("admin_sandbox", "router", "content_admin"),
    ("config_users", "router", "multi_tenancy"),
    ("config_user_distill", "router", "content_admin"),
    ("config_teams", "router", "multi_tenancy"),
    ("config_roles", "router", "multi_tenancy"),
    ("config_invites", "router", "multi_tenancy"),
    ("config_security", "router", "system_config"),
    # Internal callback surface of the security management plugin
    # (security-manager): security_ops MCP → double-gated read-only log queries
    ("internal_security", "router", "audit"),
    # All endpoints are CONFIG_TOKEN admin operations (including writes +
    # connectivity tests), no public reads → the whole module belongs to EE
    ("service_configs", "router", "system_config"),
    # "Database tools" data source management (create/update/delete applies
    # immediately: render dbhub.toml + restart sidecar + tool wiring)
    ("data_sources", "router", "system_config"),
    # "Metadata governance": table/column semantics + enum dictionaries +
    # golden SQL (improves accuracy of direct-to-DB data retrieval)
    ("db_metadata", "router", "system_config"),
    ("team_files", "router", "multi_tenancy"),
    ("admin_marketplace", "router", "content_admin"),
    ("admin_agent_marketplace", "router", "content_admin"),
    ("admin_plugins", "router", "content_admin"),
    # Marketplace visibility scope: brief lists of subjects (users/teams/roles),
    # data source for the admin console's visibility scope picker
    ("admin_visibility", "router", "content_admin"),
    ("config_license", "router", None),
    # External model gateway control plane (LiteLLM Proxy): issue/revoke
    # virtual keys, read usage
    ("gateway_admin", "router", "model_gateway"),
    # External gateway Anthropic-protocol data endpoints (self-built translation
    # layer → litellm OpenAI upstream): public endpoints (virtual keys are
    # validated by litellm, not CONFIG_TOKEN), for Claude Code / Cherry Studio
    # agent access
    ("gateway_anthropic", "router", "model_gateway"),
)


def iter_edition_routers(specs):
    """Yield (module name, router, feature bit|None) per registry entry; skip missing modules."""
    from importlib import import_module

    for spec in specs:
        module_name, attr = spec[0], spec[1]
        feature = spec[2] if len(spec) > 2 else None
        try:
            module = import_module(f"{__name__}.{module_name}")
        except ModuleNotFoundError:
            continue
        yield module_name, getattr(module, attr), feature


__all__ = [
    "mock_sso_router",
    "login_router",
    "CE_ROUTERS",
    "EE_ROUTERS",
    "iter_edition_routers",
]
