"""Config API - expose frontend display name mappings."""

from fastapi import APIRouter

router = APIRouter(prefix="/v1/config", tags=["Config"])


@router.get("/tool-names", summary="获取工具中文名称映射")
async def get_tool_display_names():
    """
    返回工具函数名到中文显示名称的映射字典。
    前端在工具调用卡片和能力中心使用该映射来展示可读的中文名称。
    """
    from core.config.display_names import TOOL_DISPLAY_NAMES, MCP_SERVER_DISPLAY_NAMES
    return {
        "tools": TOOL_DISPLAY_NAMES,
        "servers": MCP_SERVER_DISPLAY_NAMES,
    }
