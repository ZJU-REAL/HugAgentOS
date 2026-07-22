"""MySpace virtual-filesystem tools (write/read/edit/ls/move/... over sandbox).

Extracted from the oversized core/llm/tool.py; ``core.llm.tool`` re-exports
``register_myspace_tools``.
"""

import json
import logging
from typing import Any, Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse
from core.llm.tools.edition_myspace import (
    find_organization_project_artifact,
    list_organization_project_files,
    project_subtree_folder_ids,
    register_organization_tools,
)
from core.services.artifact_edition import (
    artifact_scope_fields,
    artifact_scope_folder_id,
    extend_artifact_item,
)

logger = logging.getLogger(__name__)


def register_myspace_tools(
    toolkit: Toolkit,
    user_id: Optional[str] = None,
    scope: Optional["ProjectScope"] = None,  # type: ignore[name-defined]
) -> None:
    """Register MySpace access tools for Lab code execution sessions.

    ``scope`` (project mode): pass a :class:`ProjectScope`.
    - personal scopes use the shared UserFolder implementation;
    - edition-specific scopes delegate to code that isn't shipped in Community Edition;
    - no scope (non-project conversation): personal behavior unchanged.
    """
    if not user_id:
        return

    import base64 as _b64
    import json

    from core.sandbox import SandboxError as _SandboxError
    from core.sandbox import StageFile as _StageFile
    from core.sandbox import get_sandbox_provider as _get_provider

    def _project_subtree_folder_ids(db: Any) -> Optional[set]:
        """In project mode, compute the full subtree folder_id set of the linked folder (root included).

        Returns None outside project mode; in project mode with no root_folder_id, returns an empty set (→ nobody is allowed).
        """
        if scope is None:
            return None
        root = scope.root_folder_id
        if not root:
            return set()
        organization_subtree = project_subtree_folder_ids(db, scope)
        if organization_subtree is not None:
            return organization_subtree

        from core.db.models import UserFolder

        out: set = set()
        stack = [root]
        guard = 0
        while stack and guard < 5000:
            guard += 1
            fid = stack.pop()
            if fid in out:
                continue
            out.add(fid)
            rows = (
                db.query(UserFolder.folder_id)
                .filter(
                    UserFolder.user_id == user_id,
                    UserFolder.parent_folder_id == fid,
                    UserFolder.deleted_at.is_(None),
                )
                .all()
            )
            stack.extend(r[0] for r in rows)
        return out

    async def list_myspace_files(
        folder_id: str = "",
        file_type: str = "all",
        keyword: str = "",
        limit: int = 20,
    ) -> ToolResponse:
        """列出当前"我的空间"项目范围中的内容（子文件夹 + 文件）。

        每次调用返回当前所在位置的直接子文件夹（`sub_folders`）和文件（`items`）
        —— 想浏览嵌套结构时递归调用即可（拿到 `sub_folders[i].folder_id` 后再次调用本工具）。

        Args:
            folder_id (`str`):
                文件夹 ID。留空（默认）即当前作用域的根：
                  - 非项目对话：个人 MySpace 根
                  - 个人项目：挂钩 UserFolder
                  - 其他版本项目：由版本扩展实现
            file_type (`str`):
                文件过滤：'all'（默认） / 'image' / 'document'。
            keyword (`str`):
                按文件名/标题模糊搜索。
            limit (`int`):
                返回文件条数上限，默认 20，最大 100。

        Returns:
            JSON 文本：`{folder, sub_folders, items, total}`。
            - `folder`：当前所在文件夹元信息 `{folder_id, name}`；位于根目录时为 null。
            - `sub_folders`：当前层级的直接子文件夹列表 `[{folder_id, name}, ...]`。
            - `items`：当前层级的文件 `[{artifact_id, name, type, mime_type, size_bytes, source, ...}]`。
            - `total`：当前过滤条件下的文件总数（不含 sub_folders 计数）。
        """
        try:
            from core.db.engine import SessionLocal
            from core.db.models import UserFolder
            from core.db.repository import ArtifactRepository

            limit = min(int(limit), 100)
            mime_prefix: Optional[str] = None
            if file_type == "image":
                mime_prefix = "image/"
            elif file_type == "document":
                mime_prefix = "document"

            db = SessionLocal()
            try:
                # Project mode: default root → linked_folder_id; any out-of-scope folder_id is rejected outright
                _subtree = _project_subtree_folder_ids(db)
                if _subtree is not None:
                    if not folder_id:
                        folder_id = (scope.root_folder_id if scope is not None else "") or ""
                    elif folder_id not in _subtree:
                        raise ValueError("项目模式下不能访问挂钩文件夹之外的文件夹")

                organization_listing = list_organization_project_files(
                    db,
                    scope=scope,
                    folder_id=folder_id,
                    mime_prefix=mime_prefix,
                    keyword=keyword,
                    limit=limit,
                )
                if organization_listing is not None:
                    folder_info, sub_folders, items_rows, total = organization_listing
                else:
                    folder_info: Optional[dict[str, Any]] = None
                    if folder_id:
                        current = (
                            db.query(UserFolder)
                            .filter(
                                UserFolder.folder_id == folder_id,
                                UserFolder.user_id == user_id,
                                UserFolder.deleted_at.is_(None),
                            )
                            .first()
                        )
                        if current is None:
                            raise ValueError(f"文件夹 {folder_id} 不存在")
                        folder_info = {
                            "folder_id": current.folder_id,
                            "name": current.name,
                        }
                    sub_folder_rows = (
                        db.query(UserFolder)
                        .filter(
                            UserFolder.user_id == user_id,
                            UserFolder.parent_folder_id == (folder_id or None),
                            UserFolder.deleted_at.is_(None),
                        )
                        .order_by(UserFolder.name.asc())
                        .all()
                    )
                    sub_folders = [
                        {"folder_id": folder.folder_id, "name": folder.name}
                        for folder in sub_folder_rows
                    ]
                    from core.db.repository import ROOT_FOLDER_SENTINEL

                    repo = ArtifactRepository(db)
                    items_rows, total = repo.list_by_user_with_chat(
                        user_id=user_id,
                        mime_prefix=mime_prefix,
                        keyword=keyword or None,
                        page=1,
                        page_size=limit,
                        folder_id=folder_id or ROOT_FOLDER_SENTINEL,
                    )
            finally:
                db.close()

            items = []
            for row in items_rows:
                art = row["artifact"]
                extra = art.extra_data or {}
                source = extra.get("source", "ai_generated")
                if source not in ("user_upload", "code_exec"):
                    if extra.get("source") == "user_upload":
                        source = "user_upload"
                    elif art.artifact_id.startswith("ua_"):
                        source = "user_upload"
                    else:
                        source = "ai_generated"
                item = {
                    "artifact_id": art.artifact_id,
                    "name": art.filename or art.title,
                    "title": art.title,
                    "type": art.type,
                    "mime_type": art.mime_type,
                    "size_bytes": art.size_bytes,
                    "source": source,
                    "user_folder_id": art.user_folder_id,
                    "chat_title": row.get("chat_title"),
                    "created_at": art.created_at.isoformat() if art.created_at else None,
                }
                items.append(extend_artifact_item(art, item))

            payload = {
                "folder": folder_info,
                "sub_folders": sub_folders,
                "total": total,
                "items": items,
            }
            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))],
            )
        except Exception as exc:
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=json.dumps({"error": str(exc)}, ensure_ascii=False))
                ],
            )

    async def stage_myspace_file(artifact_id: str) -> ToolResponse:
        """将"我的空间"中的文件暂存到代码执行工作区。"""
        try:
            from core.content.artifact_refs import resolve_artifact_storage_key
            from core.db.engine import SessionLocal
            from core.db.repository import ArtifactRepository
            from core.storage.factory import get_storage

            db = SessionLocal()
            try:
                from core.db.models import Artifact as ArtifactModel
                from sqlalchemy import func

                organization_handled, art = find_organization_project_artifact(
                    db, scope=scope, reference=artifact_id
                )
                if not organization_handled:
                    repo = ArtifactRepository(db)
                    art = repo.get_by_id(artifact_id)
                if not art and not organization_handled:
                    _q = db.query(ArtifactModel).filter(
                        ArtifactModel.deleted_at.is_(None),
                        func.lower(ArtifactModel.filename) == func.lower(artifact_id),
                        ArtifactModel.user_id == user_id,
                    )
                    art = _q.order_by(ArtifactModel.created_at.desc()).first()

                if art:
                    if not organization_handled and art.user_id != user_id:
                        raise PermissionError("无权访问该文件")
                    # Project mode: the artifact must live within the linked folder subtree
                    _subtree2 = _project_subtree_folder_ids(db)
                    if _subtree2 is not None:
                        _folder_col = artifact_scope_folder_id(scope, art)
                        if not _folder_col or _folder_col not in _subtree2:
                            raise PermissionError("项目模式下不能 stage 项目沙盒外的文件")
                    storage_key = resolve_artifact_storage_key(art.artifact_id, art.storage_key)
                    if not storage_key:
                        raise ValueError(f"文件 {artifact_id} 缺少有效的存储地址")
                    if storage_key != art.storage_key:
                        art.storage_key = storage_key
                        db.commit()
                    filename = art.filename or art.title or "file"
                    mime_type = art.mime_type or "application/octet-stream"
                    size_bytes = art.size_bytes or 0
                else:
                    # Try 3: fall back to the file-index artifact store.
                    # Office/MCP tools persist there and return that file_id;
                    # the file only lands in the DB once the agent explicitly
                    # pins it. Mirror the DB→file-index fallback that
                    # _resolve_artifact_files / resolve_artifact_to_b64 use.
                    from core.artifacts.store import get_artifact as _store_get_artifact

                    item = _store_get_artifact(artifact_id)
                    if not item:
                        raise ValueError(f"文件 {artifact_id} 不存在或已删除")
                    item_user = (item.get("metadata") or {}).get("user_id")
                    if item_user and item_user != user_id and not organization_handled:
                        raise PermissionError("无权访问该文件")
                    filename = item.get("name") or "file"
                    mime_type = item.get("mime_type") or "application/octet-stream"
                    size_bytes = int(item.get("size") or 0)
                    storage_key = resolve_artifact_storage_key(
                        artifact_id,
                        item.get("storage_key"),
                    )
                    if not storage_key:
                        raise ValueError(f"文件 {artifact_id} 缺少有效的存储地址")
                    # Backfill a DB Artifact row so the file becomes visible in
                    # "我的空间" (list_myspace_files) and movable into a folder.
                    # Best-effort: a backfill failure must not block staging.
                    try:
                        from core.content.artifact_refs import infer_artifact_type

                        scope_fields = artifact_scope_fields(scope)

                        exists = (
                            db.query(ArtifactModel.artifact_id)
                            .filter(
                                ArtifactModel.artifact_id == artifact_id,
                            )
                            .first()
                        )
                        if not exists:
                            db.add(
                                ArtifactModel(
                                    artifact_id=artifact_id,
                                    user_id=user_id,
                                    type=infer_artifact_type(mime_type),
                                    title=filename,
                                    filename=filename,
                                    size_bytes=max(size_bytes, 1),
                                    mime_type=mime_type,
                                    storage_key=storage_key,
                                    storage_url=f"/files/{artifact_id}",
                                    extra_data={
                                        "source": "ai_generated",
                                        "tool_name": "stage_myspace_file",
                                    },
                                    **scope_fields,
                                )
                            )
                            db.commit()
                    except Exception as _bf_exc:  # noqa: BLE001
                        db.rollback()
                        logger.warning(
                            "stage_myspace_file artifact backfill failed: %s",
                            _bf_exc,
                        )
            finally:
                db.close()

            storage = get_storage()
            file_bytes = storage.download_bytes(storage_key)
            content_b64 = _b64.b64encode(file_bytes).decode()

            try:
                provider = _get_provider()
                staged = await provider.stage_files(
                    user_id,
                    [_StageFile(name=filename, content_b64=content_b64)],
                )
            except _SandboxError as e:
                raise RuntimeError(f"暂存失败：{e}") from e

            if not staged:
                raise RuntimeError("暂存失败：sandbox provider 未返回路径")

            result = {
                "path": staged[0].path,
                "name": filename,
                "size_bytes": size_bytes,
                "mime_type": mime_type,
            }
            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(result, ensure_ascii=False))],
            )
        except Exception as exc:
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=json.dumps({"error": str(exc)}, ensure_ascii=False))
                ],
            )

    async def list_favorite_chats(
        keyword: str = "",
        limit: int = 20,
    ) -> ToolResponse:
        """列出"我的空间"中收藏的会话。"""
        try:
            from core.db.engine import SessionLocal
            from core.db.models import ChatMessage
            from core.db.repository import ChatSessionRepository

            limit = min(int(limit), 50)
            db = SessionLocal()
            try:
                repo = ChatSessionRepository(db)
                sessions, total = repo.list_by_user(
                    user_id=user_id,
                    favorite_only=True,
                    page=1,
                    page_size=limit,
                )

                results = []
                for s in sessions:
                    if keyword and keyword.lower() not in (s.title or "").lower():
                        continue
                    last_msg = (
                        db.query(ChatMessage)
                        .filter(
                            ChatMessage.chat_id == s.chat_id,
                            ChatMessage.role == "assistant",
                        )
                        .order_by(ChatMessage.created_at.desc())
                        .first()
                    )
                    preview = ""
                    if last_msg:
                        preview = (last_msg.content or "")[:200]

                    results.append(
                        {
                            "chat_id": s.chat_id,
                            "title": s.title or "未命名会话",
                            "created_at": s.created_at.isoformat() if s.created_at else None,
                            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                            "last_message_preview": preview,
                        }
                    )
            finally:
                db.close()

            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=json.dumps({"total": total, "items": results}, ensure_ascii=False),
                    )
                ],
            )
        except Exception as exc:
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=json.dumps({"error": str(exc)}, ensure_ascii=False))
                ],
            )

    async def get_chat_messages(
        chat_id: str,
        limit: int = 50,
    ) -> ToolResponse:
        """获取指定收藏会话的完整消息记录。"""
        try:
            from core.db.engine import SessionLocal
            from core.db.models import ChatMessage, ChatSession
            from sqlalchemy import asc

            limit = min(int(limit), 200)
            db = SessionLocal()
            try:
                session = (
                    db.query(ChatSession)
                    .filter(
                        ChatSession.chat_id == chat_id,
                        ChatSession.user_id == user_id,
                        ChatSession.deleted_at.is_(None),
                    )
                    .first()
                )
                if not session:
                    raise ValueError(f"会话 {chat_id} 不存在或无权访问")
                if not session.favorite:
                    raise PermissionError("该会话未被收藏，无法读取（仅限收藏会话）")

                messages = (
                    db.query(ChatMessage)
                    .filter(
                        ChatMessage.chat_id == chat_id,
                        ChatMessage.role.in_(["user", "assistant"]),
                    )
                    .order_by(asc(ChatMessage.created_at))
                    .limit(limit)
                    .all()
                )

                results = []
                for m in messages:
                    results.append(
                        {
                            "role": m.role,
                            "content": (m.content or "")[:5000],
                            "created_at": m.created_at.isoformat() if m.created_at else None,
                        }
                    )
            finally:
                db.close()

            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=json.dumps(
                            {"chat_id": chat_id, "messages": results}, ensure_ascii=False
                        ),
                    )
                ],
            )
        except Exception as exc:
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=json.dumps({"error": str(exc)}, ensure_ascii=False))
                ],
            )

    toolkit.register_tool_function(list_myspace_files, namesake_strategy="override")
    toolkit.register_tool_function(stage_myspace_file, namesake_strategy="override")
    toolkit.register_tool_function(list_favorite_chats, namesake_strategy="override")
    toolkit.register_tool_function(get_chat_messages, namesake_strategy="override")
    register_organization_tools(toolkit, user_id)
    logger.info("[factory] Registered MySpace tools for Lab session (user=%s)", user_id)
