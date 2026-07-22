"""Unified display-name mappings for tools and MCP servers.

Single source of truth — every module that needs a Chinese display name
for a tool or server should import from here.
"""

from __future__ import annotations

from typing import Dict

from core.config.edition_display_names import edition_tool_display_names

# ── MCP server-level names ───────────────────────────────────────────────────

# MCP server ID -> Chinese name (used for capability-center panel titles)
MCP_SERVER_DISPLAY_NAMES: Dict[str, str] = {
    "query_database": "数据库查询",
    "db_query": "数据库直连查询",
    "retrieve_dataset_content": "知识库检索",
    "internet_search": "互联网搜索",
    "ai_chain_information_mcp": "产业知识中心查询",
    "generate_chart_tool": "数据可视化",
    "report_export_mcp": "报告导出",
    # (Word capability migrated to the word-editing skill; no longer goes by an MCP tool name)
    # (Excel capability migrated to the excel-editing skill; no longer goes by an MCP tool name)
    # (PPT capability migrated to the ppt-design skill; no longer goes by an MCP tool name)
    # (PDF capability migrated to the pdf-editing skill; no longer goes by an MCP tool name)
    "web_fetch": "网站信息抓取",
    "batch_runner": "批量执行",
}

# MCP server ID -> one-line feature description (used for capability-center panel description text)
MCP_SERVER_DESCRIPTIONS: Dict[str, str] = {
    "query_database": "查询数据仓库中的行业指标与统计数值，支持自然语言提问直接获取精确数据。",
    "db_query": "通过 DBHub 网关只读直连 MySQL/PostgreSQL/SQL Server/MariaDB/SQLite 等数据库，自动探查表结构并执行 SQL 取数。",
    "retrieve_dataset_content": "从公有/私有知识库中语义检索政策文件、产业报告及用户上传文档，支持混合检索与重排序。",
    "internet_search": "通过互联网实时搜索公开网页、新闻及财经资讯，作为数据库与知识库之外的信息兜底。",
    "ai_chain_information_mcp": "获取产业链全景分析报告、核心数据指标、产业动态资讯、AI 领域热点聚合及企业画像查询。",
    "generate_chart_tool": "根据给定数据调用 Python 生成柱状图、折线图、饼图等可视化图表，结果以图片形式直接展示。",
    "report_export_mcp": "将 Markdown 格式的分析报告导出为 Word 文档，或将表格数据导出为 Excel 文件供下载。",
    # (Word capability migrated to the word-editing skill; no longer goes by an MCP tool name)
    # (Excel capability migrated to the excel-editing skill; no longer goes by an MCP tool name)
    # (PPT capability migrated to the ppt-design skill; no longer goes by an MCP tool name)
    # (PDF capability migrated to the pdf-editing skill; no longer goes by an MCP tool name)
    "web_fetch": "抓取指定网页 URL 的内容，提取正文文本或 Markdown，支持搜索引擎结果页解析。",
    "batch_runner": "对一组对象（Excel 行/多份文档/文本枚举）批量执行同一个任务；先生成可确认的计划，用户审阅模板后再逐条执行。",
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
    "retrieve_local_kb": "私有知识库检索",
    "list_datasets": "查看知识库列表",
    "internet_search": "互联网搜索",
    "get_chain_information": "产业链分析",
    "get_industry_news": "产业资讯",
    "get_latest_ai_news": "AI 热点聚合",
    "get_industry_hot_companies": "领域热门企业榜",
    "get_industry_hot_products": "领域热门产品榜",
    "get_company_hot_events": "企业舆情事件",
    "get_product_detail": "产品情报详情",
    "search_company": "企业搜索",
    "get_company_base_info": "企业基本信息",
    "get_company_business_analysis": "企业经营分析",
    "get_company_tech_insight": "企业技术洞察",
    "get_company_funding": "企业资金穿透",
    "get_company_risk_warning": "企业风险预警",
    "generate_chart_tool": "数据可视化",
    "export_table_to_excel": "导出 Excel 表格",
    # (Word capability migrated to the word-editing skill, see src/backend/skill_bundles/word-editing/)
    # The MCP layer no longer exposes word_mcp; the scripts/*.py CLIs inside the skill are the single entry point.
    # (Excel capability migrated to the excel-editing skill, see src/backend/skill_bundles/excel-editing/)
    # The MCP layer no longer exposes excel_mcp; the skill's scripts/excel-cli is the single entry point.
    # (PPT capability migrated to the ppt-design skill, see src/backend/skill_bundles/ppt-design/)
    # The MCP layer no longer exposes ppt_mcp; the skill's scripts/ppt-cli is the single entry point.
    # (PDF capability migrated to the pdf-editing skill, see src/backend/skill_bundles/pdf-editing/)
    # The MCP layer no longer exposes pdf_mcp; the skill's scripts/pdf-cli is the single entry point.
    # Industry-chain bundle sub-tools (legacy workflow.py mapping)
    "get_ai_chain_information": "区块链信息查询",
    "get_latest_ai_chain_info": "最新区块链动态",
    "get_ai_chain_news": "区块链新闻搜索",
    # Batch execution
    "batch_plan": "批量执行计划",
    # Built-in tools
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
    "bash": "执行 Shell 命令",
    "Bash": "执行 Shell 命令",  # Title-cased alias for models that follow the Read/Edit/Write naming family
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
    "list_myspace_files": "浏览我的空间",
    "stage_myspace_file": "导入文件到工作区",
    "list_favorite_chats": "浏览收藏会话",
    "get_chat_messages": "读取会话记录",
    **edition_tool_display_names(),
}
