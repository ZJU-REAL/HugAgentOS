"""Write tool — create / fully overwrite sandbox files, ported from Claude Code's FileWriteTool.

Invariants:
- **Existing files must be Read first**: path missing from state -> reject (prevents blind overwrite)
- **No external change**: state compared via sha256 (same as Edit)

myspace auto-persistence (B + in-place + immediate sync):
- When the path is under ``/myspace/...`` or ``/workspace/myspace/{user_id}/...``:
  * Same-named artifact exists -> overwrite its storage content (**same file_id**, Canvas iframe needs no URL change)
  * None exists -> create a new artifact
  * Also write the bytes into the backend's ``myspace_cache/{user_id}/`` so the next fresh sandbox seed sees them
- When the path is under ``/workspace/scratch/`` or other ``/workspace/`` subdirectories:
  * No sync by default — this is the "intermediate output" area
  * The model can explicitly pass ``register_as_artifact=True`` to register an output at any path as a downloadable
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
    sandbox_exec_bash,
    shell_quote,
    upsert_myspace_artifact,
)
from ._myspace_confirm import OP_WRITE
from ._paths import (
    PATH_POLICY_DOC,
    basename,
    is_myspace_physical,
    parent_dir,
    to_physical_path,
    validate_project_scope_path,
    validate_workspace_path,
)
from ._state import ReadEntry, ReadStateTracker

logger = logging.getLogger(__name__)


# Write produces UTF-8 text; saving it under these extensions is guaranteed to yield broken fake documents
_BINARY_DOC_EXTS = {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf"}


def _make_unified_diff(path: str, old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
        lineterm="",
    ))


def register_write(
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

    async def Write(
        file_path: str,
        content: str,
        register_as_artifact: bool = False,
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

        if not isinstance(content, str):
            return resp_json({"error": "content 必须是字符串"})

        # ── Hard guard against binary document formats ───────────────────
        # Write can only produce UTF-8 text; saving text as binary formats like
        # .docx/.xlsx/.pptx/.pdf yields fake documents that cannot be opened/parsed
        # (real incident: a docx artifact was overwritten in place with markdown
        # text and the frontend failed to read it). Reject for both create and overwrite.
        _ext = basename(file_path).rsplit(".", 1)[-1].lower() if "." in basename(file_path) else ""
        if _ext in _BINARY_DOC_EXTS:
            return resp_json({
                "error": (
                    f"Write 只能写 UTF-8 纯文本，不能直接生成 .{_ext} 二进制文档"
                    "（产物会是无法打开的假文档）。请用 bash 调命令行工具生成"
                    "（docx 用 python-docx，xlsx 用 openpyxl，pdf 用 reportlab "
                    "等），生成后写到同一路径即可自动同步。"
                ),
            })

        # ── Logical path (/myspace/...) -> physical path (/workspace/myspace/<uid>/...) ──
        physical = to_physical_path(file_path, user_id)
        is_persistent = is_myspace_physical(physical, user_id)

        _g = await myspace_write_guard(
            chat_id=chat_id, op=OP_WRITE, logical_path=file_path,
            is_myspace=bool(is_persistent), interactive=interactive,
            summary=f"写入 {file_path}（{len(content)} 字符）",
        )
        if _g is not None:
            return _g

        # ── Existing-file detection + invariant ─────────────────────────
        provider = _get_provider()
        existing: Optional[bytes] = None
        try:
            existing = await provider.get_file(_sess, physical, user_id=user_id)
        except _SE:
            existing = None
        except _SCE as exc:
            return resp_json({"error": f"沙盒连接失败: {exc}"})

        # Absent from the sandbox != file does not exist: a myspace file may not yet be
        # materialized into the sandbox (sandbox rebuild / provider without bind mount).
        # In that case the artifact must be the authority, otherwise "overwriting an
        # existing file" gets misjudged as "create" and bypasses the read-before-write
        # protection below.
        if existing is None and is_persistent and user_id:
            try:
                existing = await _ms.materialize_into_sandbox(
                    provider, _sess, user_id, file_path, scope=scope,
                )
            except Exception as mexc:  # noqa: BLE001
                logger.warning("[write] myspace 物化失败 %s: %s", file_path, mexc)

        is_update = existing is not None
        original_text: Optional[str] = None

        if is_update:
            entry = state.get(physical)
            if entry is None:
                # Also accept that the model Read'd via the logical path
                entry = state.get(file_path)
            if entry is None:
                return resp_json({
                    "error": (
                        f"{file_path} 已存在，必须先 Read 该文件再 Write "
                        "（防止覆盖你未读过的内容）。"
                    ),
                })
            if entry.parsed_doc:
                return resp_json({
                    "error": (
                        f"{file_path} 是二进制文档（docx/pdf/xlsx/pptx），Read "
                        "返回的是它的**解析文本**，用 Write 覆盖会损坏文档。"
                        "请用 bash 调命令行工具（python-docx 等）重新生成。"
                    ),
                })
            if entry.offset is not None:
                return resp_json({
                    "error": (
                        f"上次 Read({file_path}) 只读了部分内容；Write 之前"
                        "需要先完整 Read（不传 offset/limit）。"
                    ),
                })
            cur_sha = hashlib.sha256(existing).hexdigest()
            if cur_sha != entry.sha256:
                state.forget(physical)
                state.forget(file_path)
                return resp_json({
                    "error": (
                        f"{file_path} 在 Read 之后被外部修改了，"
                        "请先重新 Read 再 Write。"
                    ),
                })
            try:
                original_text = existing.decode("utf-8")
            except UnicodeDecodeError:
                original_text = None

        # ── Create parent directory (inside the sandbox) ─────────────────
        pd = parent_dir(physical)
        if pd and pd != "/workspace":
            mk_exit, _, mk_err = await sandbox_exec_bash(
                f"mkdir -p {shell_quote(pd)}",
                chat_id=_sess, timeout=10,
            )
            if mk_exit != 0:
                return resp_json({
                    "error": f"创建父目录 {pd} 失败: {mk_err}",
                })

        # ── Write into the sandbox ─────────────────────────────────────
        new_bytes = content.encode("utf-8")
        try:
            await provider.put_file(_sess, physical, new_bytes, user_id=user_id)
        except (_SE, _SCE) as exc:
            return resp_json({"error": f"写入失败: {exc}"})

        # ── Update state (keyed by logical path so the model can Read/Edit with the same path next time) ──
        new_sha = hashlib.sha256(new_bytes).hexdigest()
        state.record(file_path, ReadEntry(
            content=new_bytes, sha256=new_sha, offset=None, limit=None,
        ))
        # Also record the physical path so a later Read/Edit that mixes logical/physical paths still hits
        if physical != file_path:
            state.record(physical, ReadEntry(
                content=new_bytes, sha256=new_sha, offset=None, limit=None,
            ))

        # ── Reverse sync: myspace paths auto-persist; other paths follow register_as_artifact ──
        artifact_ref: Optional[dict] = None
        if is_persistent and user_id:
            # Folder-aware reverse sync: create the UserFolder chain per
            # /myspace/<folder>/<filename> and set user_folder_id, so the "My Space"
            # directory structure flows back faithfully.
            artifact_ref = _ms.sync_upsert(
                user_id=user_id,
                chat_id=chat_id,
                logical_path=file_path,
                content=new_bytes,
                scope=scope,
            )
        elif register_as_artifact and user_id:
            artifact_ref = upsert_myspace_artifact(
                user_id=user_id,
                chat_id=chat_id,
                filename=basename(physical),
                content=new_bytes,
                scope=scope,
            )

        # Auto-pin to the workspace: makes the card appear in the current turn without the model explicitly calling pin_to_workspace
        if artifact_ref:
            pin_artifact_to_workspace(artifact_ref)

        # ── Response ───────────────────────────────────────────────────
        payload: dict = {
            "ok": True,
            "type": "update" if is_update else "create",
            "file_path": file_path,   # hand back the path the model originally passed in
            "physical_path": physical,
            "size": len(new_bytes),
            "persistent": is_persistent,
        }
        if is_update and original_text is not None:
            payload["diff"] = _make_unified_diff(file_path, original_text, content)
        if artifact_ref:
            payload["artifact"] = artifact_ref
            payload["artifacts"] = [artifact_ref]
            payload["file_id"] = artifact_ref.get("file_id")
            if artifact_ref.get("in_place_update"):
                payload["note"] = (
                    "已就地更新 my-space 中的同名 artifact（file_id 不变，"
                    "Canvas/下载链接保持有效）。"
                )
        return resp_json(payload)

    Write.__doc__ = (
        "创建文件或全量覆盖已存在文件。\n\n"
        + PATH_POLICY_DOC + "\n\n"
        "本工具说明：\n"
        "- 默认写沙盒：``/workspace/scratch/<name>`` 放中间产物/调试输出，\n"
        "  用完即弃、不影响用户。一般工作都写这里。\n"
        "- 仅当用户明确要求把东西存进他「我的空间」时，才写\n"
        "  ``/myspace/<文件夹>/<文件名>``：路径里的文件夹层级会忠实映射到\n"
        "  我的空间目录树（缺失文件夹自动建），写入**立即**同步到我的空间，\n"
        "  用户在对应文件夹可见/可下载，同名文件保持同一 file_id。\n"
        "- 写在 ``/workspace/`` 下的文件要让用户能下载，用\n"
        "  ``sandbox_get_artifact`` 或传 ``register_as_artifact=True``。\n\n"
        "前置条件：\n"
        "- 现存文件必须先 ``Read`` 完整读过（不传 offset/limit），否则被拒——\n"
        "  避免盲写抹掉你不了解的内容。\n"
        "- 优先用 ``Edit``：小修改只发 diff 比 Write 全量重写省 token。\n\n"
        "Args:\n"
        "    file_path (`str`): 文件路径。默认沙盒 ``/workspace/scratch/<name>``；\n"
        "        仅在用户要求存入其「我的空间」时用 ``/myspace/...``。\n"
        "    content (`str`): 完整文件内容（UTF-8 文本）。\n"
        "    register_as_artifact (`bool`): 默认 false。写在沙盒里又想让用户\n"
        "        能下载该文件时传 true。写到 ``/myspace/`` 下不需要（自动同步）。\n\n"
        "Returns:\n"
        "    JSON: ``{ok: true, type: 'create'|'update', file_path, physical_path,\n"
        "             size, persistent, diff?, artifact?, file_id?, note?}``\n"
        "    成功；``{error: '...'}`` 失败。\n"
        "    ``persistent=true`` 表示文件已进入「我的空间」并跨会话保留。\n"
    )

    toolkit.register_tool_function(Write, namesake_strategy="override")
    logger.info("[factory] Registered Write tool (chat_id=%s)", chat_id)
