"""Execution switches read from system config: code capability, turbo mode, planning."""
from __future__ import annotations
from core.config.store import system_config
from core.config.store import system_config_constants

def code_capability_enabled() -> bool:
    """Master switch for the main agent's code execution capability (single source of truth at runtime).

    Sole control source = the DB value of Config console "System config" (plan B, env fallback retired);
    seeded explicitly to "true". Saving in the console calls invalidate_cache; takes effect ≤30s without restart.
    agent_factory and the admin_prompts preview call the same source, ensuring the console display matches runtime.
    """
    try:
        val = system_config.SystemConfigService.get_instance().get("sandbox.code_capability_enable", "false")
    except Exception:  # noqa: BLE001 — conservatively disable on config-layer errors
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def turbo_mcp_server_ids() -> frozenset[str]:
    """极速模式装配的 MCP server 集合（运行时唯一真源）。

    控制源 = Config 管理台「系统配置 → 极速模式」的 ``turbo.mcp_server_ids``
    （逗号分隔）。刻意不与能力目录（catalog）的启停求交集——极速模式的工具面
    是独立的产品契约。配置异常或解析为空时回退到内置检索三件套。
    """
    default = ",".join(sorted(system_config_constants.DEFAULT_TURBO_MCP_SERVER_IDS))
    try:
        raw = system_config.SystemConfigService.get_instance().get("turbo.mcp_server_ids", default) or default
    except Exception:  # noqa: BLE001 — conservatively fall back to the built-in trio
        return system_config_constants.DEFAULT_TURBO_MCP_SERVER_IDS
    ids = frozenset(part.strip() for part in str(raw).split(",") if part.strip())
    return ids or system_config_constants.DEFAULT_TURBO_MCP_SERVER_IDS


def turbo_skill_ids() -> tuple[str, ...]:
    """极速模式装配的技能集合（``AdminSkill.skill_id`` / 内置技能 id 列表）。

    控制源 = Config 管理台「系统配置 → 极速模式」的 ``turbo.skill_ids``（逗号
    分隔）。与用户本轮显式呼唤的技能合并去重。默认空 —— 不选就没有技能，
    极速模式的系统提示词里也就不出现技能清单。
    """
    try:
        raw = system_config.SystemConfigService.get_instance().get("turbo.skill_ids", "") or ""
    except Exception:  # noqa: BLE001 — 配置层异常时按「没配技能」处理
        return ()
    return tuple(dict.fromkeys(part.strip() for part in str(raw).split(",") if part.strip()))


def turbo_plugin_ids() -> tuple[str, ...]:
    """极速模式装配的插件集合（``InstalledPlugin.install_id`` 列表）。

    控制源 = Config 管理台「系统配置 → 极速模式」的 ``turbo.plugin_ids``（逗号
    分隔）。插件是「技能 + 工具」的能力包，装配时由 agent_factory 展开成其组件
    技能 / MCP，与 ``turbo.mcp_server_ids`` 合并去重。默认空 —— 不选就没有插件。
    """
    try:
        raw = system_config.SystemConfigService.get_instance().get("turbo.plugin_ids", "") or ""
    except Exception:  # noqa: BLE001 — 配置层异常时按「没配插件」处理
        return ()
    return tuple(dict.fromkeys(part.strip() for part in str(raw).split(",") if part.strip()))


def turbo_manual_invoke_enabled() -> bool:
    """极速模式下是否允许显式呼唤（斜杠技能 / @子智能体 / 插件）临时装配。"""
    try:
        val = system_config.SystemConfigService.get_instance().get("turbo.manual_invoke_enable", "true")
    except Exception:  # noqa: BLE001
        return True
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def turbo_max_iters() -> int:
    """极速模式的 ReAct 迭代硬上限（最小 2，防止配错把回答轮也掐掉）。"""
    try:
        val = system_config.SystemConfigService.get_instance().get("turbo.max_iters", "4")
        return max(2, int(str(val).strip()))
    except Exception:  # noqa: BLE001
        return 4


def auto_plan_entry_enabled() -> bool:
    """Master switch for the main agent's in-conversation plan tracker (the update_plan tool).

    Control source = the Config console "System config" DB value ``chat.auto_plan_entry_enable``,
    defaulting to "true" (enabled by default). Ops can set it to false to disable the capability, ≤30s, no restart.
    Conservatively disable on config-layer errors (don't register the tool, fall back to manually clicking "Plan mode").
    """
    try:
        val = system_config.SystemConfigService.get_instance().get("chat.auto_plan_entry_enable", "true")
    except Exception:  # noqa: BLE001 — conservatively disable on config-layer errors
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "on")
