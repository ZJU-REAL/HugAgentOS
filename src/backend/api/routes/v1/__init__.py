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
    # 作业编排：沙箱里的作业脚本经 internal_jobs 回调后端（建台账、派子智能体、报终态），
    # jobs 是给人看的只读进度视图（输入框上方那条状态条）。两条都必须在——CE 的
    # agent_factory 已经注册了 run_job 工具，少了回调路由，作业会在第一发回调 404 当场死掉。
    ("internal_jobs", "router"),
    ("jobs", "router"),
    ("internal_sites", "router"),
    ("projects", "router"),
    ("api_keys", "router"),
    ("me_capabilities", "router"),
    ("marketplace", "router"),
    ("mcp_marketplace", "router"),
    ("agent_marketplace", "router"),
    ("plugins", "router"),
    ("integrations", "router"),
    ("channels", "router"),
    ("meta", "router"),
    # 个人进化的证据面与偏好开关（结算查询 / prefs / 个人候选审批）。CE 完整
    # 可用；控制面（管理员审批/发布）在 EE。此前 CE 漏挂本路由，首启向导的
    # PATCH /v1/evolution/prefs 落到桌面本机 server 的 GET catch-all 上，
    # 「启动进化」直接 405。
    ("evolution", "router"),
    ("sites", "router"),
    ("desktop", "router"),
    ("local", "router"),
    # 工具执行权限档（标准 / 只读 / 自动）——输入框工具栏那颗权限胶囊的数据源
    ("tool_approval", "router"),
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
