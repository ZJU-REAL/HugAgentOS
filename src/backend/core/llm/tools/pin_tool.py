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
        """将一组文件加入"工作区"——**唯一**让文件出现在对话区给用户看到的方式。

        ⚠️ **强制规则，适用于任何工具产生的任何文件类型**：
        - 工具产生的文件**默认隐藏**，不会自动出现在对话区。
        - **只有**通过本工具 pin 过的文件才会作为附件展示给用户。
        - 没 pin = 用户看不到，哪怕你已经成功生成了文件。

        **何时必须调用**：用户要求生成/产出**任何**可交付文件时（文档、图片、
        PPT、Excel、PDF、CSV、压缩包、音视频……），完成生成后**必须**调用一次
        本工具，把所有要交付给用户的最终文件 ID 一次性传入。这是一个
        **收尾步骤**，不是可选项。

        **一次传入所有文件，不要分多次调用**：
        - 同时输出 Word + Excel + 图表？→ ``pin_to_workspace(file_ids=["fid_word","fid_xlsx","fid_png"])``
          一次搞定，**不要**调用三次
        - 单个文件？→ 也用列表：``pin_to_workspace(file_ids=["fid_only"])``
        - 重复调用会累加（已 pin 的去重），但应当一次性 pin 完。

        覆盖场景（不限于以下，凡涉及"产出文件"都适用）：
        - **Word/PPT/Excel/PDF 技能产物**：``word-cli`` / ``ppt-cli`` /
          ``excel-cli`` / ``pdf-cli`` 生成或编辑后，经 ``sandbox_get_artifact``
          登记得到的最终 ``file_id``；中间稿不 pin
        - **PDF**：``pdf-cli merge`` / ``pdf-cli split`` / ``pdf-cli create`` /
          ``pdf-cli reformat`` 等子命令返回的最终结果
        - **图表 / 图像**：代码执行（``bash`` 跑 Python/可视化脚本）生成
          的最终图片、可视化文件；调试中的草图不 pin
        - **数据导出**：技能脚本经 ``bash`` + ``sandbox_get_artifact`` 登记的
          .csv / .json / .zip 等结果文件
        - **基于用户上传文件的加工产物**：pin 加工结果，不 pin 用户原文件

        如果用户没要求文件输出（纯文字回答），就不要调用本工具。

        Args:
            file_ids (`List[str]`):
                要固定的 artifact 文件 ID 列表。每个 ID 来自前面任意工具返回
                结果中的 ``file_id`` 字段，或用户上传文件的 ``ua_*`` ID。
                即使只 pin 一个文件也必须传列表（如 ``["fid_xxx"]``）。

        Returns:
            JSON: ``{ok: true, pinned: [{file_id, name, already_pinned}, ...],
                     failed: [{file_id, error}, ...], pinned_count}``。
            ``ok`` 为 false 仅在入参完全无效时；部分 file_id 无效不会让 ok=false，
            它们会出现在 ``failed`` 字段里。
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
