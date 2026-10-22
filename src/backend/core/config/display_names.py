"""Unified display-name mappings for tools and MCP servers.

Single source of truth — every module that needs a Chinese display name
for a tool or server should import from here.
"""

from __future__ import annotations

from typing import Dict

from core.config.edition import (
    edition_mcp_server_descriptions,
    edition_mcp_server_display_names,
    edition_tool_display_names,
)

# ── MCP server-level names ───────────────────────────────────────────────────

# MCP server ID -> Chinese name (used for capability-center panel titles)
MCP_SERVER_DISPLAY_NAMES: Dict[str, str] = {
    "query_database": "数据库查询",
    "db_query": "数据库直连查询",
    "retrieve_dataset_content": "知识库检索",
    "internet_search": "互联网搜索",
    # (Word/Excel/PPT capability migrated to the officecli-* skills, PDF to pdf-editing;
    #  none of them goes by an MCP tool name any more)
    "web_fetch": "网站信息抓取",
    **edition_mcp_server_display_names(),
}

# MCP server ID -> one-line feature description (used for capability-center panel description text)
MCP_SERVER_DESCRIPTIONS: Dict[str, str] = {
    "query_database": "查询数据仓库中的行业指标与统计数值，支持自然语言提问直接获取精确数据。",
    "db_query": "通过 DBHub 网关只读直连 MySQL/PostgreSQL/SQL Server/MariaDB/SQLite 等数据库，自动探查表结构并执行 SQL 取数。",
    "retrieve_dataset_content": "从公有/私有知识库中语义检索政策文件、产业报告及用户上传文档，支持混合检索与重排序。",
    "internet_search": "检索公开网页、官方文档、新闻及财经资讯，支持多查询并发、来源去重和原文核验。",
    # (Word/Excel/PPT capability migrated to the officecli-* skills, PDF to pdf-editing;
    #  none of them goes by an MCP tool name any more)
    "web_fetch": "抓取指定网页 URL 的内容，提取正文文本或 Markdown，支持搜索引擎结果页解析。",
    **edition_mcp_server_descriptions(),
}

# ── Tool function-level names ────────────────────────────────────────────────

# Tool function name -> Chinese display name (used for chat tool cards + streaming events)
TOOL_DISPLAY_NAMES: Dict[str, str] = {
    # MCP tools
    "publish_site": "发布站点",
    "query_database": "数据库查询",
    "execute_sql": "执行 SQL 查询",
    "search_objects": "探查库表结构",
    "retrieve_dataset_content": "公有知识库检索",
    "retrieve_local_kb": "本地知识库检索",
    "list_datasets": "查看知识库列表",
    # LLM Wiki / 概念图谱（仅 Wiki-capable 知识库后端下暴露）
    "wiki_overview": "知识地图总览",
    "wiki_locate": "知识地图定位",
    "wiki_read_page": "阅读概念页",
    "wiki_expand": "展开相关概念",
    "wiki_fetch_source": "回溯原文出处",
    "internet_search": "互联网搜索",
    # ── 插件工具 ────────────────────────────────────────────────────────────
    # 商业版插件的工具展示名走版本接缝 ``edition_tool_display_names()``（见文件
    # 末尾的合并），不直接写在这张 CE 共享表里——CE 派生的品牌门禁禁止这些
    # 工具名出现在共享文件中。
    # (Word/Excel/PPT capability migrated to the officecli-* skills under
    #  src/backend/extensions/skills/default/; they run the pre-installed `officecli`
    #  binary through Bash, so word_mcp / excel_mcp / ppt_mcp no longer exist.)
    # (PDF capability migrated to the pdf-editing skill, see src/backend/extensions/skills/default/pdf-editing/)
    # The MCP layer no longer exposes pdf_mcp; the skill's scripts/pdf-cli is the single entry point.
    # Built-in tools
    "ask_user_question": "询问用户",
    "get_skills": "查询可用技能",
    "get_agents": "查询可用智能体",
    "get_mcp_tools": "查询 MCP 工具列表",
    "search_knowledge_base": "知识库搜索",
    # Sub-agent dispatch
    "call_subagent": "调用子智能体",
    # Skill system
    "view_text_file": "读取文件",
    "web_fetch": "网页抓取",
    # Cross-turn file access
    "read_artifact": "读取文件内容",
    # Workspace file visibility
    "pin_to_workspace": "固定到工作区",
    # Code-execution Lab tools
    "Bash": "执行 Shell 命令",
    "Read": "读取文件",
    "Edit": "编辑文件",
    "Write": "写入文件",
    "Glob": "查找文件",
    "Grep": "搜索内容",
    "sandbox_put_artifact": "上传文件到沙箱",
    "sandbox_get_artifact": "从沙箱保存文件",
    # Deprecated (kept for fallback display)
    "execute_code": "代码执行（已废弃）",
    "run_command": "执行命令（已废弃）",
    # Deprecated (kept for fallback display)
    "use_skill": "加载技能（已废弃）",
    # My Space access tools (code-execution mode)
    **edition_tool_display_names(),
}
