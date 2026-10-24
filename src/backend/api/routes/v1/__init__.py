"""Community-edition route registry."""

from importlib import import_module

from .mock_sso import login_router
from .mock_sso import router as mock_sso_router

CE_ROUTERS: tuple[tuple[str, str], ...] = (
    ("chats", "router"),
    ("chat_forks", "router"),
    ("auth", "router"),
    ("users", "router"),
    ("user_preferences", "router"),
    ("catalog", "router"),
    ("features.knowledge.api.kb", "router"),
    ("summary", "router"),
    ("classify", "router"),
    ("config", "router"),
    ("file_parse", "router"),
    ("file_upload", "router"),
    ("content", "router"),
    ("memories", "router"),
    ("ontologies", "router"),
    ("models", "router"),
    ("audio", "router"),
    ("prompt_management", "router"),
    ("chat_shares", "router"),
    ("agent_responses", "router"),
    ("agent_api_keys", "router"),
    ("project_api_keys", "router"),
    ("api.edition", "router"),
    ("artifacts", "router"),
    ("space_files", "router"),
    ("space_previews", "router"),
    ("plans", "router"),
    ("loops", "router"),
    ("features.automation.api.automations", "router"),
    ("chat_runs", "router"),
    ("me_system", "router"),
    ("me_logs", "router"),
    ("myspace_folders", "router"),
    # 作业编排：沙箱里的作业脚本经 internal_jobs 回调后端（建台账、派子智能体、报终态），
    # jobs 是给人看的只读进度视图（输入框上方那条状态条）。两条都必须在——CE 的
    # agent_factory 已经注册了 run_job 工具，少了回调路由，作业会在第一发回调 404 当场死掉。
    ("internal_jobs", "router"),
    ("jobs", "router"),
    ("commands", "router"),
    ("features.sites.api.internal_sites", "router"),
    ("features.hosted_apps.api.applications", "router"),
    ("features.workbench.api.workbench", "router"),
    ("features.hosted_apps.api.application_transports", "internal_router"),
    ("features.hosted_apps.api.application_transports", "public_router"),
    ("features.hosted_apps.api.application_transports", "mcp_router"),
    ("project_environments", "router"),
    ("projects", "router"),
    ("api_keys", "router"),
    ("me_capabilities", "router"),
    ("features.marketplace.api.marketplace", "router"),
    ("features.marketplace.api.mcp_marketplace", "router"),
    ("features.marketplace.api.agent_marketplace", "router"),
    ("features.workbench.api.plugin_ui", "router"),
    ("plugin_resources", "router"),
    ("plugins", "router"),
    ("integrations.api.integrations", "router"),
    ("integrations.channels.api.channels", "router"),
    ("integrations.channels.api.channel_desktop", "router"),
    ("meta", "router"),
    # 个人进化的证据面与偏好开关（结算查询 / prefs / 个人候选审批）。CE 完整
    # 可用；控制面（管理员审批/发布）在 EE。此前 CE 漏挂本路由，首启向导的
    # PATCH /v1/evolution/prefs 落到桌面本机 server 的 GET catch-all 上，
    # 「启动进化」直接 405。
    ("evolution", "router"),
    ("features.sites.api.sites", "router"),
    ("features.desktop.api.desktop", "router"),
    ("features.desktop.api.desktop_login", "router"),
    # The desktop shell bridge and device capability management are CE runtime APIs.
    ("features.desktop.api.desktop_capability", "router"),
    ("features.desktop.api.desktop_observability", "router"),
    ("features.desktop.api.desktop_capabilities", "router"),
    # This computer's link to a Personal Agent on the cloud it signed in to.
    ("features.desktop.api.device_link", "router"),
    ("features.desktop.api.local", "router"),
    ("features.sites.api.local_site_sources", "router"),
    # 工具执行权限档（标准 / 只读 / 自动）——输入框工具栏那颗权限胶囊的数据源
    ("tool_approval", "router"),
)

EE_ROUTERS: tuple = ()


def iter_edition_routers(entries):
    for entry in entries:
        module_name, attr, *feature_items = entry
        module_path = module_name if "." in module_name else f"{__name__}.{module_name}"
        module = import_module(module_path)
        feature = feature_items[0] if feature_items else None
        yield module_name, getattr(module, attr), feature


__all__ = [
    "CE_ROUTERS",
    "EE_ROUTERS",
    "iter_edition_routers",
    "login_router",
    "mock_sso_router",
]
