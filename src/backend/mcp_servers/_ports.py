"""Single source of truth for MCP server → port mapping.

Both ``configs/mcp_config.py`` (which builds backend-side
``http://mcp:NNNN/mcp/`` URLs) and ``mcp_servers/_launcher.py`` (which
binds those ports inside the mcp container) read from this module.
The alembic migration ``y5z6a7b8c9d0_migrate_mcp_to_streamable_http.py``
keeps a frozen snapshot — that one is intentional.

Port assignments are stable; never reassign without updating both
catalog/display_names and any deployed configs.
"""

from __future__ import annotations

import os


def _parse_port_offset(raw: str | None, *, highest_base_port: int = 9117) -> int:
    """Parse the optional local-desktop MCP port namespace.

    Compose and ordinary server deployments leave the value unset and retain
    the stable 9100-range contract. Branded desktop shells can supply an offset
    so two products may run their private MCP meshes on the same computer.
    """

    value = (raw or "0").strip()
    try:
        offset = int(value)
    except ValueError as exc:
        raise RuntimeError(
            f"HUGAGENT_LOCAL_MCP_PORT_OFFSET 必须是整数，当前为 {value!r}"
        ) from exc
    if offset < 0 or highest_base_port + offset > 65535:
        raise RuntimeError(
            "HUGAGENT_LOCAL_MCP_PORT_OFFSET 超出有效端口范围："
            f"{offset}"
        )
    return offset


_PORT_OFFSET = _parse_port_offset(os.getenv("HUGAGENT_LOCAL_MCP_PORT_OFFSET"))

# server_id (the catalog/display_names key) → port
_BASE_PORTS: dict[str, int] = {
    "retrieve_dataset_content": 9100,  # historical KB port
    "internet_search": 9102,
    "generate_chart_tool": 9104,
    # 9105 reserved (retired report_export_mcp; document export now uses skills)
    "web_fetch": 9106,
    "batch_runner": 9107,
    "automation_task": 9108,  # 定时任务管理（插件市场可装卸；身份走 X-Current-User-Id 头）
    "skill_manager": 9112,  # 技能管理（skill-manager 插件；搜/装/创建/上架/删；身份走 X-Current-User-Id 头）
    "site_publish": 9113,  # 对话建站发布（sites 插件；把沙箱静态站发布为托管站点；身份走 X-Current-User-Id / X-Conversation-Id 头）
    "agent_manager": 9115,  # 智能体管理（agent-manager 插件；搜/装/建/改/删/上架子智能体；身份走 X-Current-User-Id 头）
    "browser_runtime": 9117,
    "plugin_manager": 9116,  # 插件管理（plugin-manager 插件；搜/装/导入/启停/卸载插件；身份走 X-Current-User-Id 头）
    # 9108 was word_mcp (retired; Word → officecli-docx skill); reused for automation_task
    # 9109 reserved (was excel_mcp; Excel capability moved into the officecli-xlsx skill)
    # 9110 reserved (was ppt_mcp; PPT capability moved into the officecli-pptx skill)
    # 9111 reserved (was pdf_mcp; PDF capability moved into pdf-editing skill)
}

PORTS: dict[str, int] = {
    server_id: port + _PORT_OFFSET for server_id, port in _BASE_PORTS.items()
}


def package_name(server_id: str) -> str:
    """Return the python package directory name for a given server_id.

    Most server_ids are also the package name; a handful that already end
    in ``_mcp`` use it verbatim. The launcher needs the package name to
    spawn ``python -m mcp_servers.<pkg>.server``.
    """
    return server_id if server_id.endswith("_mcp") else f"{server_id}_mcp"
