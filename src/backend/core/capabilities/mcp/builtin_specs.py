"""Packaged MCP catalog seed declarations."""
from typing import Any, Dict, List

BUILTIN_MCP_SERVERS: List[Dict[str, Any]] = [
    {
        "server_id": "retrieve_dataset_content",
        "display_name": "知识库检索",
        "description": "从公有/私有知识库中语义检索政策文件、产业报告及用户上传文档，支持混合检索与重排序。",
        "user_intro": None,
        "is_enabled": True,
        "sort_order": 1,
        "icon": "/home/mcp/learning.svg",
    },
    {
        "server_id": "internet_search",
        "display_name": "互联网搜索",
        "description": "检索公开网页、官方文档、新闻及财经资讯，支持多查询并发、来源去重和原文核验。",
        "user_intro": None,
        "is_enabled": True,
        "sort_order": 2,
        "icon": "/home/mcp/internet.svg",
    },
    {
        "server_id": "web_fetch",
        "display_name": "网站信息抓取",
        "description": "抓取指定网页 URL 的内容，提取正文文本或 Markdown，支持搜索引擎结果页解析。",
        "user_intro": None,
        "is_enabled": True,
        "sort_order": 6,
        "icon": "/home/mcp/source.svg",
    },
]
