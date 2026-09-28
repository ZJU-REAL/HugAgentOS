"""Packaged MCP catalog seed declarations."""
from typing import Any, Dict, List

BUILTIN_MCP_SERVERS: List[Dict[str, Any]] = [
    {
        "server_id": "query_database",
        "display_name": "数据库查询",
        "description": "查询数据仓库中的行业指标与统计数值，支持自然语言提问直接获取精确数据。",
        "user_intro": (
            "## 用途\n用自然语言向结构化数据仓库提问，自动生成查询语句并返回精确数值，"
            '避免互联网信息的不确定性。\n\n## 适用场景\n- "某年某地区的工业增加值是多少"\n'
            '- "各区今年 1–9 月固投同比"\n\n## 输出示例\n- 精确数值 + 数据口径标注\n'
            "- 时间 / 地区 / 行业等多维度数据切片\n- 同比、环比、累计值自动计算\n"
        ),
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 0,
        "icon": "/home/mcp/knowledge.svg",
    },
    {
        "server_id": "retrieve_dataset_content",
        "display_name": "知识库检索",
        "description": "从公有/私有知识库中语义检索政策文件、产业报告及用户上传文档，支持混合检索与重排序。",
        "user_intro": None,
        "is_stable": False,
        "is_enabled": True,
        "sort_order": 1,
        "icon": "/home/mcp/learning.svg",
    },
    {
        "server_id": "internet_search",
        "display_name": "互联网搜索",
        "description": "通过互联网实时搜索公开网页、新闻及财经资讯，作为数据库与知识库之外的信息兜底。",
        "user_intro": None,
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 2,
        "icon": "/home/mcp/internet.svg",
    },
    {
        "server_id": "generate_chart_tool",
        "display_name": "数据可视化",
        "description": "根据给定数据调用 Python 生成柱状图、折线图、饼图等可视化图表，结果以图片形式直接展示。",
        "user_intro": None,
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 4,
        "icon": "/home/mcp/data.svg",
    },
    {
        "server_id": "web_fetch",
        "display_name": "网站信息抓取",
        "description": "抓取指定网页 URL 的内容，提取正文文本或 Markdown，支持搜索引擎结果页解析。",
        "user_intro": None,
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 6,
        "icon": "/home/mcp/source.svg",
    },
    {
        "server_id": "batch_runner",
        "display_name": "批量执行",
        "description": (
            "对一组对象（Excel 行/多份文档/文本枚举）批量执行同一个任务；先生成可确认的计划，"
            "用户审阅模板后再逐条执行。"
        ),
        "is_stable": True,
        "is_enabled": True,
        "sort_order": 100,
        "icon": "/home/mcp/list.svg",
    },
]
