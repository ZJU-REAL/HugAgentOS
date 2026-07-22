"""Single source of truth for MCP server → port mapping（社区版：9 个通用工具）.

Both ``core/config/mcp_config.py`` (which builds backend-side
``http://mcp:NNNN/mcp/`` URLs) and ``mcp_servers/_launcher.py`` (which
binds those ports inside the mcp container) read from this module.

Port assignments are stable; never reassign without updating both
catalog/display_names and any deployed configs.
"""

from __future__ import annotations

# server_id (the catalog/display_names key) → port
PORTS: dict[str, int] = {
    "retrieve_dataset_content": 9100,  # historical KB port
    # 9101 reserved（行业数据库查询，商业版）
    "internet_search": 9102,
    # 9103 reserved（产业知识中心查询，商业版）
    "generate_chart_tool": 9104,
    "report_export_mcp": 9105,
    "web_fetch": 9106,
    "batch_runner": 9107,
    "automation_task": 9108,
    # 9109-9111 reserved (excel/ppt/pdf 已转生为 skill_bundles 技能；9108 已复用)
    "skill_manager": 9112,
    "site_publish": 9113,
}


def package_name(server_id: str) -> str:
    """Return the python package directory name for a given server_id.

    Most server_ids are also the package name; a handful that already end
    in ``_mcp`` use it verbatim. The launcher needs the package name to
    spawn ``python -m mcp_servers.<pkg>.server``.
    """
    return server_id if server_id.endswith("_mcp") else f"{server_id}_mcp"
