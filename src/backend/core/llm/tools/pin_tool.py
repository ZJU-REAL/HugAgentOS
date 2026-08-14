"""pin_to_workspace tool — pins agent-generated files as MySpace deliverables.

Extracted from the oversized ``core/llm/tool.py``. Heavy deps imported lazily
inside the function; ``core.llm.tool`` re-exports ``register_pin_to_workspace``.
"""

import logging
from typing import Any, Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse

logger = logging.getLogger(__name__)


def register_pin_to_workspace(
    toolkit: Toolkit,
    *,
    scope: Optional["ProjectScope"] = None,  # type: ignore[name-defined]
) -> None:
    """Register the ``pin_to_workspace`` tool.

    Workspace gate: by default every tool call's file_id is rendered as a
    download card. Many flows (Office editing chains in particular) emit
    intermediate file_ids that the user shouldn't see — only the final
    deliverable matters. ``pin_to_workspace`` lets the agent declare which
    file(s) to surface; once pinned at least once in a turn, only pinned
    files reach the assistant message.

    ``scope`` (project scope): the closure captures this agent run's ProjectScope and passes it
    to the internal eager ``_persist_artifacts``. It **must** be passed explicitly — this used to
    rely on a ContextVar, but the second _persist_artifacts in chats.py's finalization runs after
    the workflow's finally reset, by which point the contextvar is long empty → team-project AI
    output leaked into the personal MySpace root. Now the scope travels with the call chain, no
    timing window.
    """
    from core.services.project_scope import ProjectScope  # noqa: F401 - re-import for closure

    async def pin_to_workspace(file_ids: list[str]) -> ToolResponse:
        """把文件交付到对话区——**唯一**让用户看到文件的方式。

        凡用户要求生成/导出文件（文档、图片、PPT、Excel、PDF、CSV、压缩包、音视频、
        任何二进制产物），生成完**必须**调本工具收尾：没 pin = 用户看不到。纯文字
        回答不调。

        **一次传完所有产物**：同时给 Word+Excel+图表就一次
        ``pin_to_workspace(file_ids=["fid_a","fid_b","fid_c"])``，不要分三次；单个也传
        列表。中间稿（编辑链里的临时文件、调试草图）不要 pin。重复调用会累加、已 pin
        的自动去重，个别 ID 失败不影响其余文件交付。

        **file_id 从哪来**：沙盒里生成的文件先 ``sandbox_get_artifact`` 登记拿 ID；
        ``generate_chart_tool`` 等工具和 word/ppt/excel/pdf-cli 直接返回 ID；用户上传或
        我的空间里的文件用 ``list_myspace_files`` 拿 ID。**只传 ID，不要传路径或文件名**
        （含 ``/`` 或 ``.docx`` 就是错的）。

        Args:
            file_ids (`List[str]`):
                artifact 文件 ID 列表，取自前面工具返回的 ``file_id``，或用户上传
                文件的 ``ua_*`` ID。只 pin 一个也要传列表（``["fid_xxx"]``）。

        Returns:
            JSON: ``{ok, pinned: [{file_id, name, already_pinned}], failed: [{file_id, error}], pinned_count}``。
            ``ok=false`` 仅在入参完全无效时；个别 ID 失败只出现在 ``failed`` 里。
        """
        import json as _json

        from core.artifacts.store import get_artifact
        from core.llm import workspace as _workspace

        # Mark the gate active even if every id below fails — the agent's
        # *intent* to use the workspace is what flips the default. Otherwise
        # a list of bad ids would silently revert to "show everything".
        _workspace.mark_active()

        # Normalize input: tolerate a stray bare string too, but the
        # docstring says "always a list". Empty / non-list / non-string
        # entries get rejected with a clear error.
        if isinstance(file_ids, str):
            raw_ids: list[Any] = [file_ids]
        elif isinstance(file_ids, list):
            raw_ids = file_ids
        else:
            return ToolResponse(content=[TextBlock(
                type="text",
                text=_json.dumps(
                    {"ok": False, "error": "file_ids 必须是字符串列表，例如 [\"fid_a\",\"fid_b\"]"},
                    ensure_ascii=False,
                ),
            )])

        if not raw_ids:
            return ToolResponse(content=[TextBlock(
                type="text",
                text=_json.dumps(
                    {"ok": False, "error": "file_ids 不能为空列表"},
                    ensure_ascii=False,
                ),
            )])

        pinned_results: list[Dict[str, Any]] = []
        failed_results: list[Dict[str, Any]] = []
        to_persist: list[Dict[str, Any]] = []

        for raw in raw_ids:
            fid = str(raw or "").strip() if isinstance(raw, str) else ""
            if not fid:
                failed_results.append({"file_id": str(raw), "error": "file_id 为空或非字符串"})
                continue

            try:
                item = get_artifact(fid)
            except Exception as exc:
                logger.warning("pin_to_workspace: get_artifact(%s) failed: %s", fid, exc)
                item = None

            if not item:
                failed_results.append({"file_id": fid, "error": f"artifact {fid} 不存在或无权访问"})
                continue

            added = _workspace.pin(
                file_id=fid,
                name=item.get("name"),
                mime_type=item.get("mime_type"),
                size=item.get("size"),
                url=f"/files/{fid}",
            )
            pinned_results.append({
                "file_id": fid,
                "name": item.get("name"),
                "already_pinned": not added,
            })
            to_persist.append({
                "file_id": fid,
                "name": item.get("name"),
                "mime_type": item.get("mime_type"),
                "size": item.get("size"),
                "storage_key": item.get("storage_key"),
                "url": f"/files/{fid}",
                "tool_name": "pin_to_workspace",
            })

        # Persist pinned files to the DB ``artifacts`` table NOW — not only
        # at run finalization. Otherwise in-run MySpace ("我的空间") tools (Move /
        # stage_myspace_file / list_myspace_files) which resolve against the
        # DB can't see a file the agent just pinned (it only exists in the
        # file-index store + in-memory workspace until the run ends).
        # The deferred _persist_artifacts at run end dedups by artifact_id,
        # so this never double-inserts. Best-effort: failure must not break
        # the pin.
        try:
            from core.infra.logging import chat_id_var, user_id_var

            _uid = user_id_var.get() or ""
            _cid = chat_id_var.get() or ""
            if _uid and to_persist:
                from core.db.engine import SessionLocal
                from core.services.artifact_service import persist_artifacts

                _db = SessionLocal()
                try:
                    persist_artifacts(_db, _uid, _cid or None, to_persist, scope=scope)
                finally:
                    _db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pin_to_workspace: eager DB persist failed: %s", exc)

        result: Dict[str, Any] = {
            "ok": True,
            "pinned": pinned_results,
            "pinned_count": len(_workspace.get_pinned_file_ids()),
        }
        if failed_results:
            result["failed"] = failed_results
        return ToolResponse(content=[TextBlock(
            type="text",
            text=_json.dumps(result, ensure_ascii=False),
        )])

    toolkit.register_tool_function(pin_to_workspace, namesake_strategy="override")
    logger.info("[factory] Registered pin_to_workspace tool")


__all__ = ["register_pin_to_workspace"]
