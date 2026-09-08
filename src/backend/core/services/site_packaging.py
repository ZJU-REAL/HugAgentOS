"""Pack a sandbox site directory and unpack a publish archive.

Shared by the internal ``site_publish`` callback route and the desktop hybrid
upload path, so it carries no HTTP concerns: callers translate the returned
error string or the raised ``ValueError`` into their own transport.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import uuid
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_PACK_BYTES = (
    40 * 1024 * 1024
)  # tar archive cap (a separate 30MB total quota applies after unpacking)
UNPACK_MAX_FILES = 400  # unpack fuse (service layer caps at 300; slightly looser here)


def resolve_project_context(chat_id: str, user_id: str):
    """Resolve the current source project, including transferred team projects."""
    if not chat_id:
        return None, None
    from fastapi import HTTPException
    from core.db.engine import SessionLocal
    from core.db.models import ChatSession, Project
    from core.auth.permissions_iface import resolve_project_permission
    from core.services.project_scope import build_project_ctx
    from core.llm.tools._paths import to_physical_path

    with SessionLocal() as db:
        chat = db.get(ChatSession, chat_id)
        if chat is None or not chat.project_id:
            return None, None
        if chat.user_id != user_id:
            raise HTTPException(403, "发布站点需要使用自己的项目会话")
        project = db.get(Project, chat.project_id)
        if project is None or project.deleted_at is not None:
            raise HTTPException(409, "站点源码项目不存在")
        if resolve_project_permission(db, user_id, project) not in ("edit", "admin"):
            raise HTTPException(403, "当前项目不允许编辑或发布站点")
        ctx = build_project_ctx(db, project.project_id)
        folder = (ctx or {}).get("project_folder_name")
        if not folder:
            raise HTTPException(409, "源码项目未绑定有效空间文件夹")
        if project.kind == "team":
            from core.llm.tools.project_working_copy import directory
            return project.project_id, directory(project.project_id)
        return project.project_id, to_physical_path(f"/myspace/{folder}", user_id)


async def pack_and_fetch_dir(
    src: str,
    _sess: Optional[str],
    user_id: str,
    *,
    extra_excludes: Tuple[str, ...] = (),
) -> Tuple[Optional[List[Tuple[str, bytes]]], Optional[str]]:
    """tar the directory inside the sandbox → fetch it back → safely unpack. Returns exactly one of (files, error)."""
    from pathlib import Path
    from core.sandbox import ExecuteRequest, get_sandbox_provider
    from core.sandbox import SandboxError, SandboxConnectError
    from core.sandbox import directory_archive
    from core.services.site_service import MAX_SITE_FILE_BYTES, MAX_SITE_TOTAL_BYTES

    provider = get_sandbox_provider()
    archive_name = f".__site_pack_{uuid.uuid4().hex}.tgz"
    archive_path = "/workspace/" + archive_name
    script_name = archive_name + ".py"
    cleanup_name = archive_name + ".cleanup.py"
    options = {
        "source": src,
        "archive_name": archive_name,
        "excludes": [".git", "node_modules", "__pycache__", ".hugagent-source-manifest.json", *extra_excludes],
        "max_files": UNPACK_MAX_FILES,
        "max_file_bytes": MAX_SITE_FILE_BYTES,
        "max_total_bytes": MAX_SITE_TOTAL_BYTES,
        "max_archive_bytes": MAX_PACK_BYTES,
    }
    # Persistent OpenSandbox commands do not forward ExecuteRequest.params to
    # stdin. Embed the JSON as a Python string literal so every provider runs
    # the same portable archive program with the same inputs.
    program = (
        "import io, sys\n"
        f"sys.stdin = io.StringIO({json.dumps(options, ensure_ascii=False)!r})\n"
        + Path(directory_archive.__file__).read_text(encoding="utf-8")
    )
    request = ExecuteRequest(
        script_content=program,
        script_name=script_name,
        language="python",
        session_id=_sess,
        user_id=user_id,
        timeout=60,
    )
    try:
        result = await provider.execute(request)
        if result.exit_code:
            return None, f"打包目录失败（{src}）: {result.stderr or result.stdout}"
        data = await provider.get_file(_sess, archive_path, user_id=user_id)
        if not data:
            return None, "站点发布包为空"
        return safe_extract_tar(data), None
    except (SandboxError, SandboxConnectError, tarfile.TarError, ValueError) as exc:
        return None, f"站点打包失败: {exc}"
    finally:
        try:
            cleanup = await provider.execute(ExecuteRequest(
                script_content=(
                    "from pathlib import Path\n"
                    f"for path in {(archive_path, '/workspace/' + script_name, '/workspace/' + cleanup_name)!r}:\n"
                    "    Path(path).unlink(missing_ok=True)\n"
                ),
                script_name=cleanup_name,
                language="python", session_id=_sess, user_id=user_id, timeout=15,
            ))
            if cleanup.exit_code:
                logger.error("site archive cleanup failed: %s", cleanup.stderr)
        except (SandboxError, SandboxConnectError):
            logger.exception("site archive cleanup transport failed")


def safe_extract_tar(data: bytes) -> List[Tuple[str, bytes]]:
    """Unpack a tar.gz in memory; returns a list of (relative path, content).

    Only regular files are accepted; symlinks/hardlinks/device files are dropped outright
    (guarding against symlink escape). Absolute paths and ``..`` traversal are re-checked by
    the service layer's normalize_rel_path.
    """
    from core.services.site_service import MAX_SITE_FILE_BYTES, MAX_SITE_TOTAL_BYTES

    if len(data) > MAX_PACK_BYTES:
        raise ValueError("站点发布包过大")
    files: List[Tuple[str, bytes]] = []
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf:
            if not member.isreg():
                continue
            # Note: must not use lstrip("./") — that's a character-set strip and would peel ".npmrc" into "npmrc"
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if not name or name.startswith("/") or ".." in name.split("/"):
                raise ValueError("非法站点文件路径")
            if name in seen or "\\" in name:
                raise ValueError("重复或非法站点文件路径")
            seen.add(name)
            total += member.size
            if member.size > MAX_SITE_FILE_BYTES or total > MAX_SITE_TOTAL_BYTES:
                raise ValueError("站点解包后大小超限")
            if len(files) >= UNPACK_MAX_FILES:
                raise ValueError(f"站点文件数超过 {UNPACK_MAX_FILES}，请精简目录")
            fobj = tf.extractfile(member)
            if fobj is None:
                continue
            files.append((name, fobj.read()))
    return files
