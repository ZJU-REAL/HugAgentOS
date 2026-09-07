"""Pack a sandbox site directory and unpack a publish archive.

Shared by the internal ``site_publish`` callback route and the desktop hybrid
upload path, so it carries no HTTP concerns: callers translate the returned
error string or the raised ``ValueError`` into their own transport.
"""

from __future__ import annotations

import io
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
    """Conversation → the bound personal-project context.

    Returns ``(project_id, project_folder_sandbox_dir)``; returns ``(None, None)`` when the
    conversation has no bound project, the bound project is a team project, or the project does
    not belong to this user (the caller falls back to the legacy body.src_dir path).
    """
    if not chat_id:
        return None, None
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession, Project, UserFolder

        with SessionLocal() as db:
            sess = db.query(ChatSession.project_id).filter(ChatSession.chat_id == chat_id).first()
            project_id = sess[0] if sess else None
            if not project_id:
                return None, None
            proj = (
                db.query(Project)
                .filter(Project.project_id == project_id, Project.deleted_at.is_(None))
                .first()
            )
            if proj is None or proj.kind != "personal" or not proj.linked_folder_id:
                return None, None
            if proj.owner_user_id and proj.owner_user_id != user_id:
                return None, None
            row = (
                db.query(UserFolder.name)
                .filter(UserFolder.folder_id == proj.linked_folder_id)
                .first()
            )
            folder_name = row[0] if row else None
            if not folder_name:
                return None, None
            return project_id, f"/workspace/myspace/{user_id}/{folder_name}"
    except (
        Exception
    ):  # noqa: BLE001 — on resolve failure, treat as no bound project and take the legacy path
        logger.warning("[site-packaging] project context resolve failed", exc_info=True)
        return None, None


async def pack_and_fetch_dir(
    src: str,
    _sess: Optional[str],
    user_id: str,
    *,
    extra_excludes: Tuple[str, ...] = (),
) -> Tuple[Optional[List[Tuple[str, bytes]]], Optional[str]]:
    """tar the directory inside the sandbox → fetch it back → safely unpack. Returns exactly one of (files, error)."""
    from core.llm.tools._common import sandbox_exec_bash, shell_quote
    from core.sandbox import SandboxConnectError as _SandboxConnectError
    from core.sandbox import SandboxError as _SandboxError
    from core.sandbox import get_sandbox_provider as _get_provider

    excludes = (".git", "node_modules", "__pycache__") + tuple(extra_excludes)
    exclude_args = " ".join(f"--exclude={shell_quote(e)}" for e in excludes)
    pack = f"/workspace/.__site_pack_{uuid.uuid4().hex[:8]}.tgz"
    tar_cmd = (
        f"cd {shell_quote(src)} && "
        f"tar {exclude_args} -czf {shell_quote(pack)} . && "
        # ``du -b`` is a GNU extension and is unavailable on macOS/BSD.  The
        # local desktop profile runs this command on the host, so use POSIX
        # ``wc -c`` and strip its padding when parsing below.
        f"wc -c < {shell_quote(pack)}"
    )
    exit_code, stdout, stderr = await sandbox_exec_bash(tar_cmd, chat_id=_sess, timeout=60)
    if exit_code != 0:
        return None, f"打包目录失败（{src}）: {stderr or stdout}"
    try:
        pack_size = int((stdout or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        pack_size = 0
    if pack_size > MAX_PACK_BYTES:
        await sandbox_exec_bash(f"rm -f {shell_quote(pack)}", chat_id=_sess)
        return None, (
            f"目录打包后 {pack_size} bytes，超过 {MAX_PACK_BYTES} 上限，"
            "请压缩图片/清理无关文件后重试"
        )

    provider = _get_provider()
    try:
        data = await provider.get_file(_sess, pack, user_id=user_id)
    except (_SandboxError, _SandboxConnectError) as exc:
        return None, f"取回打包文件失败: {exc}"
    finally:
        try:
            await sandbox_exec_bash(f"rm -f {shell_quote(pack)}", chat_id=_sess)
        except Exception:  # noqa: BLE001 — cleanup failure does not affect the publish
            pass
    if not data:
        return None, f"打包内容为空（{src} 目录里没有文件？）"

    try:
        return safe_extract_tar(data), None
    except (tarfile.TarError, ValueError) as exc:
        return None, f"解包失败: {exc}"


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
