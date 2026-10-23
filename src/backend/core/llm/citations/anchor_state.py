"""Anchor patterns, length limits and tool lists for citations."""
from __future__ import annotations
import re
from typing import Dict, Tuple
from core.llm.citations import types

ANCHOR_RE = re.compile(r"^e(\d+)$")


LIST_FIELD_ALIASES: Tuple[str, ...] = (
    "items",
    "results",
    "events",
    "pages",
    "records",
    "entries",
    "data",
    "list",
    "rows",
    "docs",
)


TITLE_MAX = 120


SNIPPET_MAX = 3000


WHOLE_SNIPPET_MAX = 500


SKIP_TOOLS = frozenset(
    {
        # 自研文件/沙盒操作
        "Write",
        "Edit",
        "Bash",
        "write_stdin",
        "sandbox_put_artifact",
        "sandbox_get_artifact",
        "pin_to_workspace",
        "channel_read_attachment",
        "choose_design",
        "ask_user_question",
        "view_text_file",
        "get_data_context",
        "load_skill",
        "Glob",
        "Grep",
        # 产物/发布/批量
        "publish_site",
        # 定时任务写操作
        "create_scheduled_task",
        "update_scheduled_task",
        "pause_scheduled_task",
        "resume_scheduled_task",
        "delete_scheduled_task",
        "list_scheduled_tasks",
        "list_channel_conversations",
        "get_scheduled_task",
        # 技能管理
        "search_marketplace",
        "list_my_skills",
        "install_from_marketplace",
        "register_skill",
        "submit_to_marketplace",
        "delete_skill",
        "edit_skill",
        # 能力/资产发现
        "list_datasets",
        # 子智能体的引用由其内部工具的嵌套事件承载
        "call_subagent",
    }
)


TOOL_SPECS: Dict[str, types.CitationSpec] = {
    # 检索/知识类（逐条）
    "internet_search": types.CitationSpec(
        mode="list",
        items_paths=(("result", "results"), ("results",)),
        source_type="internet",
    ),
    "retrieve_dataset_content": types.CitationSpec(
        mode="list", items_paths=(("items",),), source_type="knowledge_base"
    ),
    "retrieve_local_kb": types.CitationSpec(
        mode="list", items_paths=(("items",),), source_type="knowledge_base"
    ),
    "wiki_fetch_source": types.CitationSpec(
        mode="list", items_paths=(("items",),), source_type="knowledge_base"
    ),
    "wiki_locate": types.CitationSpec(
        mode="list", items_paths=(("pages",),), source_type="knowledge_base"
    ),
    "wiki_read_page": types.CitationSpec(mode="whole", title="Wiki 页面", source_type="knowledge_base"),
    "web_fetch": types.CitationSpec(mode="whole", title="网页内容", source_type="internet"),
    # 数据/报告类（整份）
    "query_database": types.CitationSpec(mode="whole", title="数据库查询结果", source_type="database"),
}
