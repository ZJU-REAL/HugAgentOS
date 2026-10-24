"""Turn explicitly invoked skills into the load-instruction message for a turn."""

from __future__ import annotations
from core.llm.context import ir_constants
from core.llm.context import items

import logging
import typing as _typing
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


def _extract_skill_id_from_path(path: str) -> str:
    """Extract skill_id from a SKILL.md path (convention: .../skills/<skill_id>/SKILL.md)."""
    if not path:
        return ""
    import os

    norm = path.replace("\\", "/").strip().strip('"').strip("'")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return ""
    if parts[-1].upper() == "SKILL.MD" and len(parts) >= 2:
        return parts[-2]
    base = os.path.basename(norm)
    if base.upper() == "SKILL.MD" and len(parts) >= 2:
        return parts[-2]
    return ""


def _explicit_skill_ids_from_context(context: Dict[str, Any]) -> List[str]:
    skill_ids: _typing.List[str] = []
    single = context.get("skill_id")
    if single:
        skill_ids.append(str(single))
    multi = context.get("skill_ids")
    if isinstance(multi, list):
        skill_ids.extend(str(item) for item in multi if item)
    seen: set[str] = set()
    return [item for item in skill_ids if not (item in seen or seen.add(item))]


def _build_skill_injection(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build an explicit-invocation hint message (skill load instructions + MCP tool activation notice).

    Trigger sources:
    - ``skill_id`` (single skill, slash-command selection) / ``skill_ids``
      (skill list, expanded from an explicitly referenced plugin)
      → inject each skill's SKILL.md load instruction.
    - ``mcp_ids`` (MCP servers activated by an explicitly referenced plugin or
      a connector selected in the composer) → these tools are already
      force-enabled into the toolset (see _build_ctx); here we append a
      sentence telling the model they are ready and can be called on demand.

    Once enabled, ambient MCP tools stay resident and the model calls them by
    description on its own. Explicit selections guide the model toward the selected
    capabilities without forcing tool_choice or rejecting a reply without tool usage.

    Returns a dict {"role": "user", "content": "..."} or None.
    """
    skill_ids = _explicit_skill_ids_from_context(context)

    mcp_ids = [str(m) for m in (context.get("mcp_ids") or []) if m]
    connector_id = str(context.get("connector_id") or "")
    skill_id = str(context.get("skill_id") or "")
    skill_name = context.get("skill_name")
    plugin_name = context.get("plugin_name")
    connector_name = context.get("connector_name")

    if not skill_ids and not mcp_ids:
        return None

    sections: _typing.List[str] = []

    # ── Skills: load instructions ──
    if skill_ids:
        try:
            from core.capabilities.skills.loader_factory import get_skill_loader

            loader = get_skill_loader()
            metadata_all = loader.load_all_metadata()
            entries: _typing.List[str] = []
            for sid in skill_ids:
                meta = metadata_all.get(sid)
                if not meta:
                    logger.warning("[skill_inject] skill_id=%s not found", sid)
                    continue
                # Materialize once; each deployment publishes its own usable path.
                skill_dir = loader.get_skill_dir(sid)
                if not skill_dir:
                    logger.warning("[skill_inject] skill_id=%s has no skill dir", sid)
                    continue
                from core.capabilities.skills.config import model_facing_skill_dir

                sandbox_dir = model_facing_skill_dir(sid, skill_dir)
                entries.append(f'- 「{sid}」：view_text_file(file_path="{sandbox_dir}/SKILL.md")')
            if entries:
                sections.append("技能（使用技能时先读取对应说明文件）：\n" + "\n".join(entries))
        except Exception as e:  # noqa: BLE001
            logger.error("[skill_inject] failed to load skills %s: %s", skill_ids, e)

    # ── MCP: activation notice (the tools themselves are already in the toolset; call by description) ──
    if mcp_ids:
        if connector_id:
            sections.append(
                f"MCP 工具：连接器「{connector_name or connector_id}」已被用户显式选择。"
                "请优先按需使用该连接器完成相关任务，"
                "也不得用其他工具冒充该连接器的结果。"
            )
        elif plugin_name:
            sections.append(
                f"MCP 工具：插件「{plugin_name}」的 {len(mcp_ids)} 个 MCP 服务已就绪。"
                "请优先按需使用该插件的 MCP 工具或技能，"
                "使用技能时先读取对应说明文件。"
            )
        else:
            sections.append(
                f"MCP 工具：本轮显式激活的 {len(mcp_ids)} 个 MCP 连接器服务已就绪，"
                "可直接按需调用（无需加载文件），请优先使用它们完成相关任务。"
            )

    if not sections:
        return None

    # 极速模式警示：被呼唤技能的 SKILL.md 往往指示 Bash/沙箱/文件工具流程，
    # 而极速模式不装配这些工具。若不在呼唤点明说，模型会照着技能说明发起
    # 不存在的工具调用（实测会卡死在参数流上）。
    if str(context.get("chat_mode", "") or "").lower() == "turbo" and skill_ids:
        sections.append(
            "注意：当前为极速模式，没有 Bash、沙箱、文件读写和文档生成工具。"
            "技能说明中涉及这些工具的步骤一律不可执行，也不要尝试调用——"
            "读取 SKILL.md 后，只输出你能以纯文本交付的部分（结构、正文、模板等），"
            "并在结尾建议用户切换到「快速模式」或「思考模式」完整执行该技能。"
        )

    if plugin_name and connector_name:
        header = (
            f"用户已显式调用插件「{plugin_name}」并选择连接器「{connector_name}」，"
            "请优先采用这些能力："
        )
    elif plugin_name:
        header = f"用户已显式调用插件「{plugin_name}」，请优先按需采用以下能力："
    elif connector_name:
        header = f"用户已显式选择连接器「{connector_name}」，请优先采用其能力："
    elif skill_id:
        header = (
            f"用户已显式调用技能「{skill_name or skill_id}」，"
            "请优先采用该技能，使用前读取其 SKILL.md："
        )
    else:
        header = "用户已显式指定使用以下能力，请优先采用："
    content = (
        "<explicit_invocation>\n"
        + header
        + "\n"
        + "\n\n".join(sections)
        + "\n</explicit_invocation>"
    )
    item = items.make_text_context_item(
        content,
        item_id="harness:explicit_invocation",
        kind=ir_constants.KIND_REMINDER,
        origin="harness:explicit_invocation",
        trust="system",
        created_seq=0,
        priority=900,
        token_budget=8_000,
        truncation_policy=ir_constants.POLICY_HEAD_TAIL,
    )
    return {
        "role": "user",
        "content": content,
        ir_constants.SESSION_CONTEXT_META_KEY: items.session_context_metadata(item),
    }
