"""Plugin definitions responsibilities."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

PLUGIN_BUNDLES_DIR = Path(__file__).resolve().parents[3] / "plugin_bundles"


PLUGIN_SOURCE_DIRS = (PLUGIN_BUNDLES_DIR / "default", PLUGIN_BUNDLES_DIR / "marketplace")


MAX_ZIP_BYTES = 50 * 1024 * 1024  # pre-extraction cap for uploaded/imported plugin zips


DEFAULT_BOOTSTRAP_PLUGIN_SLUGS: Tuple[str, ...] = (
    "automation",
    "skill-manager",
    "sites",
)


DEFAULT_BOOTSTRAP_MARKER_ID = "default_plugins_bootstrap_v1"


LOCAL_BOOTSTRAP_MARKER_NAME = ".default-plugins-v1"


PLUGIN_MARKET_META_BLOCK_ID = "plugin_market_meta"


BUILTIN_PLUGIN_MARKET_META: Dict[str, Dict[str, Any]] = {
    "agent-manager": {"display_name": "智能体管理", "category": "效率工具"},
    "automation": {"display_name": "定时任务管理", "category": "效率工具"},
    "dingtalk": {"display_name": "钉钉工作台", "category": "办公协同"},
    "email": {"display_name": "电子邮箱", "category": "办公协同"},
    "feishu-cli": {"display_name": "飞书工作台", "category": "办公协同"},
    "firecrawl": {"display_name": "Firecrawl·网页抓取检索", "category": "信息处理"},
    "industry-knowledge-center": {"display_name": "产业知识中心", "category": "产业智能"},
    "plugin-manager": {"display_name": "插件管理", "category": "效率工具"},
    "sample-translator": {"display_name": "示例·快速翻译", "category": "办公效率"},
    "security-manager": {"display_name": "安全管理·系统自察", "category": "信息处理"},
    "sites": {"display_name": "站点·对话建站", "category": "信息处理"},
    "skill-manager": {"display_name": "技能管理", "category": "效率工具"},
    "yida": {"display_name": "宜搭低代码平台", "category": "办公协同"},
}


_META_KEYS = ("display_name", "category", "icon")


MAX_ICON_LEN = 200_000


MAX_ICON_URL_LEN = 500
