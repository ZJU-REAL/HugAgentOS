"""Edit tool — exact string replacement, ported from Claude Code's FileEditTool.

4 invariants (all required):
1. **Must Read first**: path not in state → reject
2. **Must be a full Read**: last Read used offset/limit → reject (make the model read the whole file first)
3. **Unchanged externally**: sha256 of the current file in the sandbox ≠ sha256 recorded in state → reject (make the model re-Read)
4. **old != new**: identical strings before/after replacement → reject

Uniqueness:
- ``replace_all=False`` + old_string appears multiple times in the file → reject, suggest widening context
- ``replace_all=False`` + 0 occurrences → reject

myspace auto-persistence:
After an Edit completes, if the path is under ``/myspace/`` or the physical
myspace area, reverse-sync **immediately**:
- Find the artifact with the same name → overwrite its storage content (same file_id)
- Also update myspace_cache so the next fresh sandbox seed sees it
- The artifact is updated in-place in the DB (size_bytes / updated_at); artifact_id unchanged
"""

from __future__ import annotations

import difflib
import hashlib
import logging
from typing import Optional

from agentscope.tool import Toolkit

from core.services.project_scope import ProjectScope

from . import myspace_vfs as _ms
from ._common import (
    myspace_write_guard,
    pin_artifact_to_workspace,
    resolve_sandbox_session,
    resp_json,
)
from ._myspace_confirm import OP_EDIT
from ._paths import (
    PATH_POLICY_DOC,
    is_myspace_physical,
    to_physical_path,
    validate_project_scope_path,
    validate_workspace_path,
)
from ._state import ReadEntry, ReadStateTracker

logger = logging.getLogger(__name__)


def _make_unified_diff(
    path: str, old_content: str, new_content: str, n_context: int = 3,
) -> str:
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=False),
        new_content.splitlines(keepends=False),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=n_context,
        lineterm="",
    )
    return "\n".join(diff)


def register_edit(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    state: ReadStateTracker,
    interactive: bool = True,
    project_folder_name: Optional[str] = None,
    scope: Optional[ProjectScope] = None,
) -> None:

    _sess = resolve_sandbox_session(sandbox_session_id, chat_id)

    async def Edit(
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> "ToolResponse":  # type: ignore[name-defined]
        from core.sandbox import (
            SandboxConnectError as _SCE,
            SandboxError as _SE,
            get_sandbox_provider as _get_provider,
        )

        # ── Basic input validation ───────────────────────────────────────
        path_err = validate_workspace_path(file_path)
        if path_err:
            return resp_json({"error": path_err})
        scope_err = validate_project_scope_path(file_path, project_folder_name)
        if scope_err:
            return resp_json({"error": scope_err})

        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return resp_json({"error": "old_string / new_string 必须是字符串"})

        if old_string == new_string:
            return resp_json({
                "error": "old_string 与 new_string 相同——这次 Edit 不会改变文件内容",
            })

        physical = to_physical_path(file_path, user_id)

        _g = await myspace_write_guard(
            chat_id=chat_id, op=OP_EDIT, logical_path=file_path,
            is_myspace=bool(is_myspace_physical(physical, user_id)),
            interactive=interactive,
            summary=f"编辑 {file_path}（替换片段）",
        )
        if _g is not None:
            return _g

        # ── invariant 1: must Read first (logical or physical path both fine) ──
        entry = state.get(file_path) or state.get(physical)
        if entry is None:
            return resp_json({
                "error": (
                    f"必须先 Read({file_path}) 再 Edit。Edit 工具依赖你已经知道"
                    "文件的当前内容来做精确替换。"
                ),
            })

        # ── invariant 2a: binary documents read via parsed-text fallback cannot be Edited ──
        # For docx/pdf/xlsx/pptx in "My Space", Read returns parsed text, not
        # the raw bytes — doing string replacement on the parsed text and
        # writing it back would corrupt the document outright.
        if entry.parsed_doc:
            return resp_json({
                "error": (
                    f"{file_path} 是二进制文档（docx/pdf/xlsx/pptx），Read 返回的"
                    "是它的**解析文本**，Edit 无法直接修改原文档（会损坏文件）。"
                    "请改用 bash 调命令行工具处理：docx 用 python-docx 重新生成或"
                    "修改后另存，再写回同一 /myspace 路径。"
                ),
            })

        # ── invariant 2: must be a full Read ─────────────────────────────
        if entry.offset is not None:
            return resp_json({
                "error": (
                    f"上次 Read({file_path}) 用了 offset/limit 只读了部分内容；"
                    "Edit 需要完整内容做唯一性校验。请先 Read(file_path) 不传 "
                    "offset/limit。"
                ),
            })

        # ── invariant 3: unchanged externally ──────────────────────────
        provider = _get_provider()
        try:
            current_bytes = await provider.get_file(_sess, physical, user_id=user_id)
        except (_SE, _SCE) as exc:
            return resp_json({"error": f"读取文件失败: {exc}"})

        current_sha = hashlib.sha256(current_bytes).hexdigest()
        if current_sha != entry.sha256:
            state.forget(file_path)
            state.forget(physical)
            return resp_json({
                "error": (
                    f"文件 {file_path} 在 Read 之后被外部修改了，"
                    "请重新 Read(file_path) 再 Edit。"
                ),
            })

        # ── Uniqueness / replacement ───────────────────────────────────
        try:
            current_text = current_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return resp_json({"error": "文件不是合法 UTF-8 文本，无法 Edit"})

        count = current_text.count(old_string)
        if count == 0:
            return resp_json({
                "error": (
                    "old_string 在文件中找不到。请检查空白/缩进/换行是否与 "
                    "Read 返回的内容一致（注意：行号前缀不属于内容本身）。"
                ),
            })
        if count > 1 and not replace_all:
            return resp_json({
                "error": (
                    f"old_string 在文件中出现 {count} 次，无法唯一定位。"
                    "请扩大 old_string 的上下文使其唯一，或显式传 "
                    "replace_all=true 替换所有匹配。"
                ),
            })

        if replace_all:
            new_text = current_text.replace(old_string, new_string)
            replaced = count
        else:
            new_text = current_text.replace(old_string, new_string, 1)
            replaced = 1

        # ── Write back to sandbox + update state ───────────────────────
        new_bytes = new_text.encode("utf-8")
        try:
            await provider.put_file(_sess, physical, new_bytes, user_id=user_id)
        except (_SE, _SCE) as exc:
            return resp_json({"error": f"写入失败: {exc}"})

        new_sha = hashlib.sha256(new_bytes).hexdigest()
        new_entry = ReadEntry(
            content=new_bytes, sha256=new_sha, offset=None, limit=None,
        )
        state.record(file_path, new_entry)
        if physical != file_path:
            state.record(physical, new_entry)

        # ── Reverse-sync to artifact (myspace paths only) ──────────────
        artifact_ref: Optional[dict] = None
        if is_myspace_physical(physical, user_id) and user_id:
            artifact_ref = _ms.sync_upsert(
                user_id=user_id,
                chat_id=chat_id,
                logical_path=file_path,
                content=new_bytes,
                scope=scope,
            )
            # Auto-pin: even for an in-place update with the same file_id, re-show
            # the card in the current turn (workspace state is per-turn, so an
            # Edit in a new turn always triggers a new card)
            if artifact_ref:
                pin_artifact_to_workspace(artifact_ref)

        diff = _make_unified_diff(file_path, current_text, new_text)
        payload: dict = {
            "ok": True,
            "file_path": file_path,
            "physical_path": physical,
            "replaced": replaced,
            "replace_all": replace_all,
            "diff": diff,
            "old_size": len(current_bytes),
            "new_size": len(new_bytes),
            "persistent": is_myspace_physical(physical, user_id),
        }
        if artifact_ref:
            payload["artifact"] = artifact_ref
            payload["artifacts"] = [artifact_ref]
            payload["file_id"] = artifact_ref.get("file_id")
            if artifact_ref.get("in_place_update"):
                payload["note"] = (
                    "已就地更新「我的空间」里的同名 artifact（file_id 不变，"
                    "Canvas/下载链接立即指向新内容）。"
                )
        return resp_json(payload)

    Edit.__doc__ = (
        "对文本文件做精确字符串替换。\n\n"
        + PATH_POLICY_DOC + "\n\n"
        "本工具说明：\n"
        "- 默认改沙盒里的文件（``/workspace/...``），不影响用户数据。\n"
        "- 仅当用户明确要求修改他「我的空间」里的文件时，才 Edit\n"
        "  ``/myspace/...``：改完**立即**同步回我的空间，同一 file_id，\n"
        "  下载/预览链接不变，用户立刻看到改动。\n\n"
        "前置条件（缺一不可）：\n"
        "- 必须先 ``Read(file_path)`` 完整读过该文件（不传 offset/limit）。\n"
        "- ``old_string`` 必须**精确**匹配文件内容（不含行号前缀；空白/缩进/\n"
        "  换行都要一致）。\n"
        "- 默认 ``replace_all=false`` 时，``old_string`` 必须在文件里唯一出现。\n"
        "  扩大上下文使其唯一；或传 ``replace_all=true`` 替换所有匹配。\n\n"
        "如果文件在 Read 之后被外部修改了，Edit 会失败并提示重新 Read。\n\n"
        "Args:\n"
        "    file_path (`str`): 文件路径。默认沙盒 ``/workspace/...``；仅在用户\n"
        "        要求改其「我的空间」文件时用 ``/myspace/...``。\n"
        "    old_string (`str`): 要被替换的原内容。必须与文件中现存内容精确一致。\n"
        "    new_string (`str`): 替换后的新内容。必须与 old_string 不同。\n"
        "    replace_all (`bool`): 默认 false，仅替换唯一匹配；true 则替换全部。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, file_path, physical_path, replaced, replace_all,\n"
        "             diff, old_size, new_size, persistent, file_id?, artifact?,\n"
        "             note?}`` 成功；``{error: '...'}`` 失败。\n"
    )

    toolkit.register_tool_function(Edit, namesake_strategy="override")
    logger.info("[factory] Registered Edit tool (chat_id=%s)", chat_id)
