"""MySpace virtual-filesystem tools (write/read/edit/ls/move/... over sandbox).

Extracted from the oversized core/llm/tool.py; ``core.llm.tool`` re-exports
``register_myspace_tools``.
"""

import base64
import json
import logging
from typing import Any, Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse

from core.llm.tools._tool_helpers import _resolve_artifact_files

logger = logging.getLogger(__name__)


def register_myspace_tools(
    toolkit: Toolkit,
    user_id: Optional[str] = None,
    scope: Optional["ProjectScope"] = None,  # type: ignore[name-defined]
) -> None:
    """Register MySpace access tools for Lab code execution sessions.

    ``scope`` (project mode): pass a :class:`ProjectScope`.
    - personal scope: ``list_myspace_files`` queries UserFolder + user_id;
      ``stage_myspace_file`` validates access by user_id + user_folder_id.
    - team scope: ``list_myspace_files`` queries TeamFolder + team_id;
      ``stage_myspace_file`` validates access by team_id + team_folder_id. The fallback backfill
      row also writes the correct ``team_id`` / ``team_folder_id`` per scope — avoiding the
      earlier bug where the team path only wrote user_id, producing NULL/NULL/NULL orphans that
      polluted the personal MySpace root.
    - no scope (non-project conversation): personal behavior unchanged.
    """
    if not user_id:
        return

    import base64 as _b64
    import json

    from core.sandbox import (
        SandboxError as _SandboxError,
        StageFile as _StageFile,
        get_sandbox_provider as _get_provider,
    )

    def _project_subtree_folder_ids(db: Any) -> Optional[set]:
        """In project mode, compute the full subtree folder_id set of the linked folder (root included).

        Returns None outside project mode; in project mode with no root_folder_id, returns an empty set (→ nobody is allowed).
        """
        if scope is None:
            return None
        root = scope.root_folder_id
        if not root:
            return set()
        from core.db.models import TeamFolder, UserFolder

        out: set = set()
        stack = [root]
        guard = 0
        if scope.is_personal:
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
        else:
            team_id = scope.team_id
            while stack and guard < 5000:
                guard += 1
                fid = stack.pop()
                if fid in out:
                    continue
                out.add(fid)
                rows = (
                    db.query(TeamFolder.folder_id)
                    .filter(
                        TeamFolder.team_id == team_id,
                        TeamFolder.parent_folder_id == fid,
                        TeamFolder.deleted_at.is_(None),
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
        """列出"我的空间"或团队项目挂钩文件夹中的内容（子文件夹 + 文件）。

        每次调用返回当前所在位置的直接子文件夹（`sub_folders`）和文件（`items`）
        —— 想浏览嵌套结构时递归调用即可（拿到 `sub_folders[i].folder_id` 后再次调用本工具）。

        Args:
            folder_id (`str`):
                文件夹 ID。留空（默认）即当前作用域的根：
                  - 非项目对话：个人 MySpace 根
                  - 个人项目：挂钩 UserFolder
                  - 团队项目：挂钩 TeamFolder
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
            from core.db.models import Artifact, ChatSession, TeamFolder, UserFolder
            from core.db.repository import ArtifactRepository
            from sqlalchemy import desc, or_

            limit = min(int(limit), 100)
            mime_prefix: Optional[str] = None
            if file_type == "image":
                mime_prefix = "image/"
            elif file_type == "document":
                mime_prefix = "document"

            _is_team = scope is not None and scope.is_team

            db = SessionLocal()
            try:
                # Project mode: default root → linked_folder_id; any out-of-scope folder_id is rejected outright
                _subtree = _project_subtree_folder_ids(db)
                if _subtree is not None:
                    if not folder_id:
                        folder_id = (scope.root_folder_id if scope is not None else "") or ""
                    elif folder_id not in _subtree:
                        raise ValueError(
                            "项目模式下不能访问挂钩文件夹之外的文件夹"
                        )

                # Current folder (None at the root) — personal queries UserFolder + user_id; team queries TeamFolder + team_id.
                folder_info: Optional[Dict[str, Any]] = None
                if folder_id:
                    if _is_team:
                        current = (
                            db.query(TeamFolder)
                            .filter(
                                TeamFolder.folder_id == folder_id,
                                TeamFolder.team_id == scope.team_id,
                                TeamFolder.deleted_at.is_(None),
                            )
                            .first()
                        )
                    else:
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
                    folder_info = {"folder_id": current.folder_id, "name": current.name}

                # Direct subfolders (team queries TeamFolder + team_id; personal queries UserFolder + user_id)
                if _is_team:
                    sub_folder_rows = (
                        db.query(TeamFolder)
                        .filter(
                            TeamFolder.team_id == scope.team_id,
                            TeamFolder.parent_folder_id == (folder_id or None),
                            TeamFolder.deleted_at.is_(None),
                        )
                        .order_by(TeamFolder.name.asc())
                        .all()
                    )
                else:
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
                    {"folder_id": f.folder_id, "name": f.name}
                    for f in sub_folder_rows
                ]

                # Files at the current level — team goes by team_id+team_folder_id; personal reuses the personal repo helper.
                if _is_team:
                    iq = (
                        db.query(Artifact, ChatSession.title.label("chat_title"))
                        .outerjoin(ChatSession, Artifact.chat_id == ChatSession.chat_id)
                        .filter(
                            Artifact.team_id == scope.team_id,
                            Artifact.team_folder_id == folder_id,
                            Artifact.deleted_at.is_(None),
                        )
                    )
                    if mime_prefix == "image/":
                        iq = iq.filter(Artifact.mime_type.like("image/%"))
                    elif mime_prefix == "document":
                        iq = iq.filter(~Artifact.mime_type.like("image/%"))
                    if keyword:
                        like_pattern = f"%{keyword}%"
                        iq = iq.filter(or_(
                            Artifact.filename.ilike(like_pattern),
                            Artifact.title.ilike(like_pattern),
                        ))
                    total = iq.count()
                    rows = iq.order_by(desc(Artifact.created_at)).limit(limit).all()
                    items_rows = [{"artifact": a, "chat_title": ct} for a, ct in rows]
                else:
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
                items.append({
                    "artifact_id": art.artifact_id,
                    "name": art.filename or art.title,
                    "title": art.title,
                    "type": art.type,
                    "mime_type": art.mime_type,
                    "size_bytes": art.size_bytes,
                    "source": source,
                    "user_folder_id": art.user_folder_id,
                    "team_folder_id": art.team_folder_id,
                    "chat_title": row.get("chat_title"),
                    "created_at": art.created_at.isoformat() if art.created_at else None,
                })

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
                content=[TextBlock(type="text", text=json.dumps(
                    {"error": str(exc)}, ensure_ascii=False
                ))],
            )

    async def stage_myspace_file(artifact_id: str) -> ToolResponse:
        """将"我的空间"中的文件暂存到代码执行工作区。"""
        try:
            from core.db.engine import SessionLocal
            from core.db.repository import ArtifactRepository
            from core.storage.factory import get_storage
            from core.content.artifact_refs import resolve_artifact_storage_key

            db = SessionLocal()
            try:
                from core.db.models import Artifact as ArtifactModel
                from sqlalchemy import func

                _is_team = scope is not None and scope.is_team
                repo = ArtifactRepository(db)
                art = repo.get_by_id(artifact_id)
                if not art:
                    # Filename fallback: switch the ownership column by scope, so files from teammates are findable in team projects.
                    _q = db.query(ArtifactModel).filter(
                        ArtifactModel.deleted_at.is_(None),
                        func.lower(ArtifactModel.filename) == func.lower(artifact_id),
                    )
                    if _is_team:
                        _q = _q.filter(ArtifactModel.team_id == scope.team_id)
                    else:
                        _q = _q.filter(ArtifactModel.user_id == user_id)
                    art = _q.order_by(ArtifactModel.created_at.desc()).first()

                if art:
                    # Ownership check: team mode checks team_id (membership is gated at the chat
                    # session layer); personal mode checks user_id.
                    if _is_team:
                        if art.team_id != scope.team_id:
                            raise PermissionError("无权访问该文件")
                    else:
                        if art.user_id != user_id:
                            raise PermissionError("无权访问该文件")
                    # Project mode: the artifact must live within the linked folder subtree
                    _subtree2 = _project_subtree_folder_ids(db)
                    if _subtree2 is not None:
                        _folder_col = art.team_folder_id if _is_team else art.user_folder_id
                        if not _folder_col or _folder_col not in _subtree2:
                            raise PermissionError(
                                "项目模式下不能 stage 项目沙盒外的文件"
                            )
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
                    if item_user and item_user != user_id and not _is_team:
                        # In team mode the file may have been registered by a teammate, so a plain
                        # user_id check can't block it; real ownership is enforced by the later backfill's team_id validation.
                        raise PermissionError("无权访问该文件")
                    filename = item.get("name") or "file"
                    mime_type = item.get("mime_type") or "application/octet-stream"
                    size_bytes = int(item.get("size") or 0)
                    storage_key = resolve_artifact_storage_key(
                        artifact_id, item.get("storage_key"),
                    )
                    if not storage_key:
                        raise ValueError(f"文件 {artifact_id} 缺少有效的存储地址")
                    # Backfill a DB Artifact row so the file becomes visible in
                    # "我的空间" (list_myspace_files) and movable into a folder.
                    # Best-effort: a backfill failure must not block staging.
                    try:
                        from core.content.artifact_refs import infer_artifact_type

                        # Project-scope aware: team scope writes team_id+team_folder_id;
                        # personal scope writes user_folder_id; otherwise all NULL, landing at the root.
                        _bf_user_folder: Optional[str] = None
                        _bf_team_id: Optional[str] = None
                        _bf_team_folder: Optional[str] = None
                        if _is_team:
                            _bf_team_id = scope.team_id
                            _bf_team_folder = scope.root_folder_id or None
                        elif scope is not None and scope.is_personal:
                            _bf_user_folder = scope.root_folder_id or None

                        exists = db.query(ArtifactModel.artifact_id).filter(
                            ArtifactModel.artifact_id == artifact_id,
                        ).first()
                        if not exists:
                            db.add(ArtifactModel(
                                artifact_id=artifact_id,
                                user_id=user_id,
                                user_folder_id=_bf_user_folder,
                                team_id=_bf_team_id,
                                team_folder_id=_bf_team_folder,
                                type=infer_artifact_type(mime_type),
                                title=filename, filename=filename,
                                size_bytes=max(size_bytes, 1),
                                mime_type=mime_type,
                                storage_key=storage_key,
                                storage_url=f"/files/{artifact_id}",
                                extra_data={"source": "ai_generated",
                                            "tool_name": "stage_myspace_file"},
                            ))
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
                    user_id, [_StageFile(name=filename, content_b64=content_b64)],
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
                content=[TextBlock(type="text", text=json.dumps(
                    {"error": str(exc)}, ensure_ascii=False
                ))],
            )

    async def list_favorite_chats(
        keyword: str = "",
        limit: int = 20,
    ) -> ToolResponse:
        """列出"我的空间"中收藏的会话。"""
        try:
            from core.db.engine import SessionLocal
            from core.db.repository import ChatSessionRepository
            from core.db.models import ChatMessage

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
                    last_msg = db.query(ChatMessage).filter(
                        ChatMessage.chat_id == s.chat_id,
                        ChatMessage.role == "assistant",
                    ).order_by(ChatMessage.created_at.desc()).first()
                    preview = ""
                    if last_msg:
                        preview = (last_msg.content or "")[:200]

                    results.append({
                        "chat_id": s.chat_id,
                        "title": s.title or "未命名会话",
                        "created_at": s.created_at.isoformat() if s.created_at else None,
                        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                        "last_message_preview": preview,
                    })
            finally:
                db.close()

            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(
                    {"total": total, "items": results}, ensure_ascii=False
                ))],
            )
        except Exception as exc:
            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(
                    {"error": str(exc)}, ensure_ascii=False
                ))],
            )

    async def get_chat_messages(
        chat_id: str,
        limit: int = 50,
    ) -> ToolResponse:
        """获取指定收藏会话的完整消息记录。"""
        try:
            from core.db.engine import SessionLocal
            from core.db.models import ChatSession, ChatMessage
            from sqlalchemy import asc

            limit = min(int(limit), 200)
            db = SessionLocal()
            try:
                session = db.query(ChatSession).filter(
                    ChatSession.chat_id == chat_id,
                    ChatSession.user_id == user_id,
                    ChatSession.deleted_at.is_(None),
                ).first()
                if not session:
                    raise ValueError(f"会话 {chat_id} 不存在或无权访问")
                if not session.favorite:
                    raise PermissionError("该会话未被收藏，无法读取（仅限收藏会话）")

                messages = db.query(ChatMessage).filter(
                    ChatMessage.chat_id == chat_id,
                    ChatMessage.role.in_(["user", "assistant"]),
                ).order_by(asc(ChatMessage.created_at)).limit(limit).all()

                results = []
                for m in messages:
                    results.append({
                        "role": m.role,
                        "content": (m.content or "")[:5000],
                        "created_at": m.created_at.isoformat() if m.created_at else None,
                    })
            finally:
                db.close()

            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(
                    {"chat_id": chat_id, "messages": results}, ensure_ascii=False
                ))],
            )
        except Exception as exc:
            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(
                    {"error": str(exc)}, ensure_ascii=False
                ))],
            )

    async def list_team_files(
        team_id: str = "",
        folder_id: str = "",
        file_type: str = "all",
        keyword: str = "",
        limit: int = 20,
    ) -> ToolResponse:
        """列出用户所在团队的团队文件夹内容。

        Args:
            team_id (`str`):
                团队 ID。留空时返回当前用户所在的所有团队列表（供 Agent 选择后再次调用）。
            folder_id (`str`):
                团队文件夹 ID。留空或未提供即团队根目录。
            file_type (`str`):
                过滤类型：'all' / 'image' / 'document'。
            keyword (`str`):
                按文件名/标题模糊搜索。
            limit (`int`):
                返回条数上限，默认 20，最大 100。
        """
        try:
            from core.db.engine import SessionLocal
            from core.db.models import Team, TeamMember, TeamFolder
            from core.db.repository import ArtifactRepository
            from core.auth.permissions_iface import (
                resolve_team_file_permission,
                has_permission,
            )

            limit = min(int(limit), 100)
            mime_prefix: Optional[str] = None
            if file_type == "image":
                mime_prefix = "image/"
            elif file_type == "document":
                mime_prefix = "document"

            db = SessionLocal()
            try:
                if not team_id:
                    rows = (
                        db.query(Team, TeamMember)
                        .join(TeamMember, TeamMember.team_id == Team.team_id)
                        .filter(TeamMember.user_id == user_id)
                        .order_by(Team.name.asc())
                        .all()
                    )
                    teams = [
                        {
                            "team_id": t.team_id,
                            "name": t.name,
                            "description": t.description,
                            "role": m.role,
                            "file_permission": (
                                "admin"
                                if m.role in ("owner", "admin")
                                else ("edit" if m.file_permission == "editor" else "view")
                            ),
                        }
                        for t, m in rows
                    ]
                    payload = {
                        "hint": "请指定 team_id 再次调用以查看团队文件；也可传 folder_id 浏览子目录。",
                        "teams": teams,
                    }
                    return ToolResponse(
                        content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))],
                    )

                perm = resolve_team_file_permission(db, user_id, team_id)
                if perm == "none":
                    raise PermissionError("团队不存在或你未加入")
                if not has_permission(perm, "view"):
                    raise PermissionError("当前权限不足")

                folder_info = None
                folders = []
                if folder_id:
                    folder = (
                        db.query(TeamFolder)
                        .filter(
                            TeamFolder.folder_id == folder_id,
                            TeamFolder.team_id == team_id,
                            TeamFolder.deleted_at.is_(None),
                        )
                        .first()
                    )
                    if not folder:
                        raise ValueError(f"文件夹 {folder_id} 不存在")
                    folder_info = {"folder_id": folder.folder_id, "name": folder.name}

                sub_folders = (
                    db.query(TeamFolder)
                    .filter(
                        TeamFolder.team_id == team_id,
                        TeamFolder.parent_folder_id == (folder_id or None),
                        TeamFolder.deleted_at.is_(None),
                    )
                    .order_by(TeamFolder.name.asc())
                    .all()
                )
                folders = [
                    {"folder_id": f.folder_id, "name": f.name} for f in sub_folders
                ]

                repo = ArtifactRepository(db)
                items, total = repo.list_by_team_folder(
                    team_id=team_id,
                    folder_id=folder_id or None,
                    mime_prefix=mime_prefix,
                    keyword=keyword or None,
                    page=1,
                    page_size=limit,
                )
            finally:
                db.close()

            results = []
            for row in items:
                art = row["artifact"]
                results.append({
                    "artifact_id": art.artifact_id,
                    "name": art.filename or art.title,
                    "title": art.title,
                    "type": art.type,
                    "mime_type": art.mime_type,
                    "size_bytes": art.size_bytes,
                    "team_id": art.team_id,
                    "team_folder_id": art.team_folder_id,
                    "created_at": art.created_at.isoformat() if art.created_at else None,
                })

            payload = {
                "team_id": team_id,
                "permission": perm,
                "folder": folder_info,
                "sub_folders": folders,
                "total": total,
                "items": results,
            }
            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))],
            )
        except Exception as exc:
            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(
                    {"error": str(exc)}, ensure_ascii=False
                ))],
            )

    async def stage_team_file(artifact_id: str) -> ToolResponse:
        """将团队文件夹中的文件暂存到代码执行工作区。需对所在团队具备 view 权限。

        Args:
            artifact_id (`str`):
                团队文件的 artifact_id（来自 list_team_files 返回）。
        """
        try:
            from core.db.engine import SessionLocal
            from core.db.repository import ArtifactRepository
            from core.storage.factory import get_storage
            from core.auth.permissions_iface import (
                resolve_artifact_access,
                has_permission,
            )
            from core.content.artifact_refs import resolve_artifact_storage_key

            db = SessionLocal()
            try:
                repo = ArtifactRepository(db)
                art = repo.get_by_id(artifact_id)
                if not art:
                    raise ValueError(f"文件 {artifact_id} 不存在或已删除")
                if not art.team_id:
                    raise PermissionError("该文件不属于团队文件夹，请使用 stage_myspace_file")

                # owner ∪ team composite permission (same rule as the files API): the file owner
                # can always access — one's own team files aren't locked out in CE / left-the-team scenarios
                perm = resolve_artifact_access(db, user_id, art.user_id, art.team_id)
                if perm == "none":
                    raise PermissionError("你不是该团队成员，无法访问该文件")
                if not has_permission(perm, "view"):
                    raise PermissionError("当前权限不足")

                storage_key = resolve_artifact_storage_key(art.artifact_id, art.storage_key)
                if not storage_key:
                    raise ValueError(f"文件 {artifact_id} 缺少有效的存储地址")
                if storage_key != art.storage_key:
                    art.storage_key = storage_key
                    db.commit()
                filename = art.filename or art.title or "file"
                mime_type = art.mime_type or "application/octet-stream"
                size_bytes = art.size_bytes or 0
                team_id_val = art.team_id
                team_folder_id_val = art.team_folder_id
            finally:
                db.close()

            storage = get_storage()
            file_bytes = storage.download_bytes(storage_key)
            content_b64 = _b64.b64encode(file_bytes).decode()

            try:
                provider = _get_provider()
                staged = await provider.stage_files(
                    user_id, [_StageFile(name=filename, content_b64=content_b64)],
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
                "team_id": team_id_val,
                "team_folder_id": team_folder_id_val,
            }
            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(result, ensure_ascii=False))],
            )
        except Exception as exc:
            return ToolResponse(
                content=[TextBlock(type="text", text=json.dumps(
                    {"error": str(exc)}, ensure_ascii=False
                ))],
            )

    toolkit.register_tool_function(list_myspace_files, namesake_strategy="override")
    toolkit.register_tool_function(stage_myspace_file, namesake_strategy="override")
    toolkit.register_tool_function(list_favorite_chats, namesake_strategy="override")
    toolkit.register_tool_function(get_chat_messages, namesake_strategy="override")
    toolkit.register_tool_function(list_team_files, namesake_strategy="override")
    toolkit.register_tool_function(stage_team_file, namesake_strategy="override")
    logger.info("[factory] Registered 6 MySpace/Team tools for Lab session (user=%s)", user_id)
