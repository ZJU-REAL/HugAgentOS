"""Seed values for the built-in turbo chat mode."""
from __future__ import annotations
from typing import Any, Dict
from core.db.models.chat_mode import TOOL_SCOPE_RESTRICTED
import logging
from core.chat import mode_types

logger = logging.getLogger(__name__)

def turbo_seed_fields() -> Dict[str, Any]:
    """极速模式内置行的字段，取自历史 ``turbo.*`` SystemConfig 键。

    这是那批键的**最后一次**读取——播种之后本表才是真源，Config 台
    「系统配置 → 极速模式」那组入口随之下线。
    """
    from core.config.store.execution_options import turbo_manual_invoke_enabled, turbo_max_iters, turbo_mcp_server_ids, turbo_plugin_ids, turbo_skill_ids

    try:
        mcp_ids = sorted(turbo_mcp_server_ids())
        skill_ids = list(turbo_skill_ids())
        plugin_ids = list(turbo_plugin_ids())
        manual = turbo_manual_invoke_enabled()
        iters = turbo_max_iters()
    except Exception:  # noqa: BLE001 — 配置层不可用时用内置检索三件套兜底
        logger.warning("[chat_mode] 读 turbo.* 配置失败，极速模式按内置默认播种", exc_info=True)
        mcp_ids = ["internet_search", "retrieve_dataset_content", "web_fetch"]
        skill_ids, plugin_ids, manual, iters = [], [], True, 4

    return {
        "id": "mode_builtin_turbo",
        "slug": mode_types.SLUG_TURBO,
        "name": "极速模式",
        "description": "只留检索类工具、不思考，秒级直达结果",
        "owner_user_id": None,
        "is_builtin": True,
        "enabled": True,
        "sort_order": 20,
        "tool_scope": TOOL_SCOPE_RESTRICTED,
        "mcp_server_ids": mcp_ids,
        "skill_ids": skill_ids,
        "plugin_ids": plugin_ids,
        "agent_ids": [],
        "manual_invoke_enabled": manual,
        # 极速的产品契约就是"检索直达、不执行代码"，保持关。
        "code_exec_enabled": False,
        "max_iters": iters,
        # 极速那条链路本就不思考，强度锁死——不是"默认不思考但可以改"。
        "default_effort": "fast",
        "effort_locked": True,
        "prompt_kind": "turbo",
    }
