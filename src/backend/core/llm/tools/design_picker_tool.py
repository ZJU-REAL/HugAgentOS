"""choose_design tool — pick one of three site-building design options (human-in-the-loop suspended choice).

Piggybacks on the suspension-gating skeleton of ``_myspace_confirm`` (kind=design_pick):
the tool coroutine ``await``s the user's click on the frontend DesignPickerCard, then
resumes in place once chosen. Conditionally registered by agent_factory only in
site-building chats (site-builder skill enabled).
"""

import logging
from typing import Any, Dict, List, Optional

from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse

from core.llm.tools import _myspace_confirm as _mc
from core.llm.tools._common import resp_json

logger = logging.getLogger(__name__)

_MIN_OPTIONS = 2
_MAX_OPTIONS = 4

# Corresponds to the skill name in plugin_bundles/marketplace/sites/skills/site-builder/SKILL.md.
# Plugin installation namespaces the skill id as "{slug}-{skill}[-fingerprint]" (e.g.
# sites-site-builder-a30442), so matching uses a hyphen-boundary-wrapped marker rather than
# exact equality; when renaming the skill, update this in sync, otherwise SKILL.md teaches
# the model to call choose_design while the tool is unregistered.
_SKILL_MARKER = "site-builder"


def skill_uses_choose_design(skill_id: str) -> bool:
    """Whether the skill declares using choose_design (boundary match on the namespaced id)."""
    sid = skill_id.strip().lower()
    if sid == _SKILL_MARKER:
        return True
    return (
        sid.startswith(_SKILL_MARKER + "-")
        or sid.endswith("-" + _SKILL_MARKER)
        or ("-" + _SKILL_MARKER + "-") in sid
    )


def _err(msg: str) -> ToolResponse:
    return resp_json({"ok": False, "error": msg})


def register_choose_design(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    interactive: bool = True,
) -> None:
    """Register the ``choose_design`` tool (site-building chats only).

    With ``interactive=False`` (batch/sub-agent/IM channels) the tool is still
    registered, but calls immediately return "no selection UI, decide yourself" —
    letting the same SKILL.md flow degrade gracefully in non-interactive scenarios
    instead of erroring out on a missing tool.
    """

    async def choose_design(question: str, options: List[Dict[str, Any]]) -> ToolResponse:
        """让用户从多个设计方案预览图中选择一个（**会挂起等待用户点选**）。

        仅在建站流程中使用：先为同一站点做出 2-4 个（推荐 3 个）风格迥异的
        设计 mockup（自包含单文件 HTML），用 playwright 截图后逐张经
        ``sandbox_get_artifact`` 登记得到 ``file_id``，再调用本工具。

        ⚠️ 使用规则：
        - 每个 option 的 ``image_file_id`` 必须来自 ``sandbox_get_artifact``
          返回的 ``file_id``——这些截图是选择器素材，**不要** ``pin_to_workspace``。
        - 本工具会**暂停执行等用户在界面上点选**，可能等待很久，属正常，
          不要因为耗时长而认为失败。
        - 拿到返回后必须严格按 ``selected`` 方案继续建站（布局/配色/字体
          以该 mockup 为准），禁止混入其它方案元素，禁止再次调用本工具
          重复询问同一问题。
        - 一次建站至多问一轮；用户已明确给出完整设计要求时不要调用。

        Args:
            question (`str`):
                向用户提出的选择问题，例如「您喜欢哪种设计风格？」。
            options (`List[dict]`):
                2-4 个方案（推荐 3 个），每项必须包含：
                ``id``（唯一短标识，如 "a"/"b"/"c"）、``title``（方案名，
                如「深色科技风」）、``image_file_id``（预览截图的 file_id）；
                可选 ``brief``（一句话说明该方案的布局/配色/氛围）。

        Returns:
            JSON: 选中时 ``{ok, selected_id, selected: {...}, message}``；
            用户跳过 / 超时 / 非交互时 ``selected_id`` 为空，按 ``message``
            指引自行选择方案继续。
        """
        q = (question or "").strip() or "请选择一个设计方案"
        if not isinstance(options, list) or not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
            return _err(f"options 必须是 {_MIN_OPTIONS}-{_MAX_OPTIONS} 个方案的列表（推荐 3 个）")

        from core.artifacts.store import get_artifact

        normalized: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for i, raw in enumerate(options):
            if not isinstance(raw, dict):
                return _err(f"options[{i}] 必须是对象")
            oid = str(raw.get("id") or "").strip()
            title = str(raw.get("title") or "").strip()
            fid = str(raw.get("image_file_id") or "").strip()
            if not oid or not title or not fid:
                return _err(f"options[{i}] 缺少 id/title/image_file_id 之一")
            if oid in seen_ids:
                return _err(f"options[{i}].id 重复: {oid}")
            seen_ids.add(oid)
            try:
                item = get_artifact(fid)
            except Exception:  # noqa: BLE001
                item = None
            if not item:
                return _err(
                    f"options[{i}].image_file_id 无效: {fid}——请先用 "
                    f"sandbox_get_artifact 登记截图再传其返回的 file_id"
                )
            normalized.append({
                "id": oid,
                "title": title,
                "image_file_id": fid,
                "brief": str(raw.get("brief") or "").strip(),
            })

        result = await _mc.pick(
            chat_id=chat_id,
            question=q,
            options=normalized,
            interactive=interactive,
        )

        status = result.get("status")
        if status == "chosen":
            selected = next(
                (o for o in normalized if o["id"] == result.get("option_id")), None
            )
            payload: Dict[str, Any] = {
                "ok": True,
                "selected_id": result.get("option_id"),
                "selected": selected,
                "message": (
                    f"用户选择了「{(selected or {}).get('title', '')}」方案。"
                    "请严格按该 mockup 的布局/配色/字体展开完整站点，"
                    "不要混入其它方案元素。"
                ),
            }
        elif status == "skipped":
            payload = {
                "ok": True,
                "selected_id": None,
                "status": "skipped",
                "message": (
                    "用户让你自行决定。选择你最推荐的一个方案继续建站，"
                    "并在回复中用一句话说明推荐理由。"
                ),
            }
        elif status == "timeout":
            payload = {
                "ok": False,
                "status": "timeout",
                "message": (
                    "等待用户选择超时。请选择你推荐的方案继续建站，"
                    "并在回复开头告知用户因超时已代为选择。"
                ),
            }
        else:  # blocked_non_interactive
            payload = {
                "ok": False,
                "status": status,
                "message": result.get(
                    "error", "无选择 UI，请直接选择你认为最合适的方案继续。"
                ),
            }
        return resp_json(payload)

    toolkit.register_tool_function(choose_design, namesake_strategy="override")
    logger.info("[factory] Registered choose_design tool (site-builder session)")


__all__ = ["register_choose_design", "skill_uses_choose_design"]
