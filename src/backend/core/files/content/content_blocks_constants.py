"""Block maps and schema version for content snapshots."""
from __future__ import annotations
from typing import Any
from core.config.settings import settings

BRAND_NAME = settings.branding.product_name


DOCS_BLOCK_MAP = {
    "updates": "docs_updates",
    "capabilities": "docs_capabilities",
    "prompt_hub": "prompt_hub",
    "page_config": "page_config",
    "app_config": "app_config",
}


RETIRED_BLOCK_ALIASES = {"homepage_shortcuts"}


DICT_PAYLOAD_BLOCKS = {"page_config", "prompt_versions", "app_config"}


PROMPT_VERSIONS_BLOCK_ID = "prompt_versions"


PROMPT_BLOCK_MAP = {
    "prompt_versions": PROMPT_VERSIONS_BLOCK_ID,
    "prompt_hub": "prompt_hub",
}


SNAPSHOT_SCHEMA_VERSION = 1


DEFAULT_PAGE_CONFIG: dict[str, Any] = {
    "branding": {
        "product_name": BRAND_NAME,
        "product_subtitle": "AI 智能助手",
        "logo_url": "/home/header.svg",
        "favicon_url": "/icon.png",
        "page_title": BRAND_NAME,
        "hero_title": f"你好，我是 {BRAND_NAME}",
        "hero_subtitle": "今天想从哪里开始？",
        "disclaimer": "本平台生成内容由AI大模型生成，不构成任何建议；涉及业务决策请以权威信息为准。",
    },
    "homepage": {
        # The homepage uses the transparent standalone brand mark rather than
        # the square sidebar logo, mirroring the lighter Qwen Office hero.
        "show_logo": True,
        "logo_url": "/icon.png",
        "show_suggestions": True,
        "suggested_questions": [
            "分析新能源汽车产业链的关键环节与代表企业",
            "对比两份产业政策，提炼支持方向和申报条件",
            "查询一家企业的基本情况、经营风险和产业链位置",
            "从这份材料中提炼核心观点和待办事项",
            "分析这组经营数据的趋势、异常和可能原因",
            "检索相关政策依据，并给出可核验的来源",
        ],
    },
    "navigation": {
        "panel_titles": {
            "ability_center": "能力中心",
            "skills": "技能",
            "agents": "智能体",
            "mcp": "连接器",
            "kb": "知识库",
            "docs": "更新记录",
            "app_center": "应用中心",
            "automation": "定时任务",
            "sites": "站点",
            "projects": "项目",
            "lab": "实验室",
            "settings": "系统设置",
            "my_space": "我的空间",
        },
        "panel_subtitles": {
            "ability_center": "智能体基础能力管理，包含智能体、技能、连接器与插件",
            "skills": "启用/停用技能，并查看详细介绍、输入输出与示例。",
            "agents": "选择与启用智能体，并查看其职责边界与路由提示。",
            "mcp": "管理连接器服务，并查看其作用范围与可靠性影响。",
            "kb": "浏览知识库、查看文档列表，并支持文档内检索。",
            "docs": "查看功能更新、能力中心与平台说明。",
            "app_center": "基于 AI 能力的场景化智能应用",
            "automation": "按计划自动执行任务，也可随时手动触发。在任意对话中描述你想定期做的事，即可快速创建",
            "sites": "在对话里描述需求，AI 生成完整网站并一键发布，由平台托管、凭链接即可访问",
            "projects": "把对话、文件和指令打包成专属工作空间",
            "lab": "AI 能力实验性应用",
            "settings": "",
            "my_space": "",
        },
        "admin_header": {
            "title": f"{BRAND_NAME} — 后台管理",
            "subtitle": "后台管理",
        },
        # Unified branding + per-page labels for the admin platform (content
        # management / system config / API docs) — product_name drives the header
        # brand and browser tab title of all three admin pages, replacing the old
        # admin_header.
        "admin_platform": {
            "product_name": BRAND_NAME,
            "content_label": "内容管理",
            "config_label": "系统配置",
            "apidoc_label": "接口文档",
        },
        # 与前端 utils/pageConfigDefaults.ts 的 DEFAULT_SIDEBAR_ITEMS / DEFAULT_MENU_ITEMS 保持一致。
        # 这只是新装环境的出厂值——已存在的部署以 DB 现有取值为准，由 backfill_navigation_entries
        # 补齐新增条目，管理员随后可在 /config「页面配置」里自由编排。
        "sidebar_items": ["ability_center", "automation", "sites", "my_space"],
        "menu_items": ["settings", "app_center", "projects", "lab"],
    },
    "texts": {
        "input_placeholder": "请输入您的问题…",
        "input_placeholder_agent": "请输入您的问题…",
        "search_placeholder": "搜索对话",
        "btn_new_chat": "新建对话",
        "btn_logout": "退出",
        "history_label": "历史对话",
        "sidebar_empty_state": "暂无对话记录",
        "search_no_results": "无匹配结果",
        "dialog_logout_confirm_title": "确认退出登录？",
        "dialog_logout_confirm_content": "退出登录不会丢失任何数据，重新登录后可继续使用。",
        "dialog_logout_confirm_ok": "退出登录",
        "recommend_banner_text": "",
    },
    "defaults": {
        # Initial mode on every user login / new chat:
        # - chat_mode: 'turbo' / 'fast' / 'medium' / 'high' / 'max' (recommended)
        # - thinking_mode: bool (legacy field, fallback compat; true→medium, false→fast)
        "chat_mode": "fast",
        "thinking_mode": False,
    },
    "auth": {
        # Whether the login page shows the "register" entry (tab + register subpage).
        # When off, the login page keeps only login, and backend register submissions
        # are rejected outright (not just hidden on the frontend).
        "allow_register": True,
    },
}


DEFAULT_APP_CONFIG: dict[str, Any] = {
    "apps": [],
}


DEFAULT_PROMPT_VERSIONS: dict[str, Any] = {
    "active": {},
    "versions": [],
}


NAV_BACKFILL_ENTRIES: list[dict[str, Any]] = [
    {"key": "projects", "bucket": "menu", "insert_after": "app_center"},
    {"key": "automation", "bucket": "sidebar", "insert_after": "ability_center"},
    {"key": "sites", "bucket": "sidebar", "insert_after": "automation"},
]


LEGACY_PANEL_TEXT_RENAMES: dict[str, dict[str, tuple[tuple[str, ...], str]]] = {
    "panel_titles": {
        "skills": (("技能库",), "技能"),
        "agents": (("子智能体",), "智能体"),
        "plugins": (("插件库",), "插件"),
        "mcp": (("MCP工具库", "MCP 工具库", "MCP 工具"), "连接器"),
    },
    "panel_subtitles": {
        "agents": (
            ("选择与启用子智能体，并查看其职责边界与路由提示。",),
            "选择与启用智能体，并查看其职责边界与路由提示。",
        ),
        "mcp": (
            (
                "管理 MCP 工具服务，并查看其作用范围与可靠性影响。",
                "管理 MCP 连接器服务，并查看其作用范围与可靠性影响。",
            ),
            "管理连接器服务，并查看其作用范围与可靠性影响。",
        ),
    },
}
