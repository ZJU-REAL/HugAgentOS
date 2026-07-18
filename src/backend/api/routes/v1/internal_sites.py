"""Internal site-publishing endpoint — callback target for the ``site_publish`` MCP plugin.

Why this internal endpoint exists: the ``site_publish`` MCP server runs in the **mcp container**,
which has no sandbox (sandbox provider) configuration and cannot reach the user session's sandbox
working directory. Yet "tar up the site directory inside the sandbox, fetch it back, and publish"
requires sandbox access — only the **backend container** has it. So the MCP side forwards the
``publish_site`` call **verbatim** to this endpoint, and the backend does the work:

    ``tar`` the directory inside the sandbox → fetch via ``provider.get_file`` → in-memory unpack
    (blocking symlinks / path traversal) → ``SiteService.publish`` writes each file to storage +
    persists to DB → returns the ``/site/<slug>/`` hosted URL.

Auth uses the same shared secret ``X-Internal-Token`` as ``internal_batch`` (fail-closed if
unconfigured in production). The user identity / conversation id are parsed by the MCP side from
the ``X-Current-User-Id`` / ``X-Conversation-Id`` headers and placed into the body; this endpoint
trusts only the body (already behind the internal-token gate).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
import uuid
from typing import List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from core.infra.responses import success_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/internal/sites", tags=["internal-sites"])

MAX_PACK_BYTES = 40 * 1024 * 1024  # tar archive cap (a separate 30MB total quota applies after unpacking)
_UNPACK_MAX_FILES = 400            # unpack fuse (service layer caps at 300; slightly looser here)


class PublishBody(BaseModel):
    src_dir: str = Field(
        "",
        description=(
            "沙箱里的站点根目录（在 /workspace/ 下）。项目模式（会话绑定了站点项目）"
            "下可留空，后端自动定位到项目文件夹。"
        ),
    )
    source_dir: str = Field(
        "",
        description=(
            "构建型站点（React/Vite 等）的源码工程目录。传入时：src_dir 应指向构建"
            "产物（dist），发布产物进站点存储，而**源码目录**镜像进项目文件夹"
            "（保证项目=可编辑源码工程，而不是编译产物）。静态站不传。"
        ),
    )
    title: str = ""
    slug: str = ""
    site_id: str = ""
    visibility: str = "public"
    description: str = ""
    team_id: str = ""
    user_id: str = ""   # parsed by the MCP side from X-Current-User-Id
    chat_id: str = ""   # parsed by the MCP side from X-Conversation-Id (sandbox session key)


def _check_internal_token(token: Optional[str]) -> None:
    expected = os.environ.get("BACKEND_INTERNAL_TOKEN", "")
    if not expected:
        # Same as internal_batch: reject outright when the token is unconfigured in production
        # (fail-closed); only non-production (dev) is let through for local integration testing.
        from core.config.settings import settings

        if settings.server.is_prod:
            raise HTTPException(
                status_code=503,
                detail="internal endpoint disabled: BACKEND_INTERNAL_TOKEN not configured",
            )
        return
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


def _resolve_project_context(chat_id: str, user_id: str):
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
            sess = (
                db.query(ChatSession.project_id)
                .filter(ChatSession.chat_id == chat_id)
                .first()
            )
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
    except Exception:  # noqa: BLE001 — on resolve failure, treat as no bound project and take the legacy path
        logger.warning("[internal-sites] project context resolve failed", exc_info=True)
        return None, None


def _resolve_target_site_id(project_id: str, user_id: str) -> str:
    """In project mode, resolve the live site_id already associated with the project (for publishing a new version while editing); empty string if none (create new)."""
    if not project_id:
        return ""
    try:
        from core.db.engine import SessionLocal
        from core.db.models import Site

        with SessionLocal() as db:
            row = (
                db.query(Site.site_id)
                .filter(
                    Site.project_id == project_id,
                    Site.user_id == user_id,
                    Site.deleted_at.is_(None),
                )
                .order_by(Site.created_at.asc())
                .first()
            )
            return row[0] if row else ""
    except Exception:  # noqa: BLE001
        logger.warning("[internal-sites] target site resolve failed", exc_info=True)
        return ""


def _mirror_files_to_project_folder(
    project_id: str, user_id: str, files: List[Tuple[str, bytes]]
) -> None:
    """Best-effort "mirror" of the just-published site files into the project folder (replace semantics).

    This way, whether the agent built the site in the project folder or in the /workspace/
    scratch area, after publishing the project folder always equals the live site content — the
    user can see all source files in the project, and an editing session can materialize these
    files back into the sandbox to keep working. Failures are only logged and never affect an
    already-successful publish.
    """
    if not project_id or not files:
        return
    import mimetypes

    try:
        from core.db.engine import SessionLocal
        from core.db.models import Artifact, Project, UserFolder
        from core.services.project_file_service import ProjectFileService

        with SessionLocal() as db:
            proj = (
                db.query(Project)
                .filter(Project.project_id == project_id, Project.deleted_at.is_(None))
                .first()
            )
            if (
                proj is None
                or proj.kind != "personal"
                or not proj.linked_folder_id
                or proj.owner_user_id != user_id
            ):
                return

            pfs = ProjectFileService(db)
            # 1) Clear the linked folder subtree's existing live artifacts + subfolders (keep the root folder itself)
            subtree_ids = pfs._user_subtree_ids(user_id, proj.linked_folder_id)
            if subtree_ids:
                from datetime import datetime as _dt

                now = _dt.utcnow()
                db.query(Artifact).filter(
                    Artifact.user_id == user_id,
                    Artifact.team_id.is_(None),
                    Artifact.user_folder_id.in_(subtree_ids),
                    Artifact.deleted_at.is_(None),
                ).update({Artifact.deleted_at: now}, synchronize_session=False)
                child_ids = [fid for fid in subtree_ids if fid != proj.linked_folder_id]
                if child_ids:
                    db.query(UserFolder).filter(
                        UserFolder.user_id == user_id,
                        UserFolder.folder_id.in_(child_ids),
                        UserFolder.deleted_at.is_(None),
                    ).update({UserFolder.deleted_at: now}, synchronize_session=False)
                db.commit()

            # 2) Write each file back into the project folder (preserving subpaths)
            for rel_path, content in files:
                mime, _ = mimetypes.guess_type(rel_path)
                try:
                    pfs.upload(proj, user_id, content, rel_path, mime or "text/plain")
                except Exception:  # noqa: BLE001 — one file failing does not affect the rest
                    logger.warning(
                        "[internal-sites] mirror file to project failed: %s", rel_path,
                        exc_info=True,
                    )
    except Exception:  # noqa: BLE001
        logger.warning("[internal-sites] mirror files to project folder failed", exc_info=True)


def _ensure_project_for_site(site_id: str, user_id: str, title: str) -> Optional[str]:
    """Ensure the site has a source-code workspace (personal project); returns project_id (None on failure).

    This is the key to making sites editable: **no reliance on the frontend pre-creating a project
    nor on the agent calling a tool** — at publish time the backend creates the project directly
    (named after the site title, folder with the same name) and backfills ``site.project_id``. If
    a project already exists (editing / new version), it is returned as-is. So no matter which
    path the site is published through (site panel / Yida / automation), after publishing there is
    always a correctly named, further-editable project.
    """
    if not site_id:
        return None
    try:
        from core.db.engine import SessionLocal
        from core.db.models import Project, Site
        from core.services.project_service import ProjectService

        with SessionLocal() as db:
            site = db.query(Site).filter(Site.site_id == site_id).first()
            if site is None or site.user_id != user_id:
                return None
            # Already linked and the project still exists → use it directly
            if site.project_id:
                alive = (
                    db.query(Project.project_id)
                    .filter(Project.project_id == site.project_id, Project.deleted_at.is_(None))
                    .first()
                )
                if alive:
                    return site.project_id
            # Otherwise create a new personal project named after the site title (create_personal creates a same-named folder)
            name = (title or site.title or "站点").strip()[:200] or "站点"
            proj = ProjectService(db).create_personal(
                user_id, name=name, description="对话建站源码工程（发布后可继续编辑）",
            )
            site.project_id = proj.project_id
            db.commit()
            return proj.project_id
    except Exception:  # noqa: BLE001 — project creation failure does not affect an already-successful publish
        logger.warning("[internal-sites] ensure project for site failed", exc_info=True)
        return None


def _project_root_has_package_json(project_id: str, user_id: str) -> bool:
    """Whether the project's linked folder root contains a live package.json (the marker of a build-style workspace)."""
    if not project_id:
        return False
    try:
        from core.db.engine import SessionLocal
        from core.db.models import Artifact, Project

        with SessionLocal() as db:
            proj = (
                db.query(Project)
                .filter(Project.project_id == project_id, Project.deleted_at.is_(None))
                .first()
            )
            if proj is None or not proj.linked_folder_id:
                return False
            row = (
                db.query(Artifact.artifact_id)
                .filter(
                    Artifact.user_id == user_id,
                    Artifact.team_id.is_(None),
                    Artifact.user_folder_id == proj.linked_folder_id,
                    Artifact.filename == "package.json",
                    Artifact.deleted_at.is_(None),
                )
                .first()
            )
            return row is not None
    except Exception:  # noqa: BLE001
        logger.warning("[internal-sites] package.json probe failed", exc_info=True)
        return False


async def _pack_and_fetch_dir(
    src: str,
    _sess: Optional[str],
    user_id: str,
    *,
    extra_excludes: Tuple[str, ...] = (),
) -> Tuple[Optional[List[Tuple[str, bytes]]], Optional[str]]:
    """tar the directory inside the sandbox → fetch it back → safely unpack. Returns exactly one of (files, error)."""
    from core.llm.tools._common import sandbox_exec_bash, shell_quote
    from core.sandbox import (
        SandboxConnectError as _SandboxConnectError,
        SandboxError as _SandboxError,
        get_sandbox_provider as _get_provider,
    )

    excludes = (".git", "node_modules", "__pycache__") + tuple(extra_excludes)
    exclude_args = " ".join(f"--exclude={shell_quote(e)}" for e in excludes)
    pack = f"/workspace/.__site_pack_{uuid.uuid4().hex[:8]}.tgz"
    tar_cmd = (
        f"cd {shell_quote(src)} && "
        f"tar {exclude_args} -czf {shell_quote(pack)} . && "
        f"du -b {shell_quote(pack)} | cut -f1"
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
        return _safe_extract_tar(data), None
    except (tarfile.TarError, ValueError) as exc:
        return None, f"解包失败: {exc}"


def _safe_extract_tar(data: bytes) -> List[Tuple[str, bytes]]:
    """Unpack a tar.gz in memory; returns a list of (relative path, content).

    Only regular files are accepted; symlinks/hardlinks/device files are dropped outright
    (guarding against symlink escape). Absolute paths and ``..`` traversal are re-checked by
    the service layer's normalize_rel_path.
    """
    files: List[Tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf:
            if not member.isreg():
                continue
            # Note: must not use lstrip("./") — that's a character-set strip and would peel ".npmrc" into "npmrc"
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            name = name.lstrip("/")
            if not name or name.startswith("/") or ".." in name.split("/"):
                continue
            if len(files) >= _UNPACK_MAX_FILES:
                raise ValueError(f"站点文件数超过 {_UNPACK_MAX_FILES}，请精简目录")
            fobj = tf.extractfile(member)
            if fobj is None:
                continue
            files.append((name, fobj.read()))
    return files


@router.post("/publish", summary="发布沙箱站点目录为托管站点（内部接口）")
async def publish(
    body: PublishBody,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
):
    _check_internal_token(x_internal_token)

    from core.llm.tools._common import resolve_sandbox_session
    from core.llm.tools._tool_helpers import _validate_workspace_path

    user_id = (body.user_id or "").strip()
    if not user_id:
        return success_response(data={"error": "当前会话缺少用户身份，无法发布站点"})

    # Pack-directory resolution:
    #   - Editing session (frontend bound the chat to a site project) → site files are already
    #     materialized into the project folder; pack it by default.
    #   - Site-building session (no bound project) → the agent builds in /workspace/site by default; pack it by default.
    # An explicitly passed src_dir is always respected — the agent may really have written the new
    # code there; silently rerouting would drop the new content entirely and publish the project
    # folder's old code as the new version (page "changed but looks the same").
    # Either way, after publishing _ensure_project_for_site + _mirror land the files into the (new or existing) project.
    project_id, project_dir = _resolve_project_context(body.chat_id or "", user_id)
    src = (body.src_dir or "").strip().rstrip("/")
    if not src or src == ".":
        src = project_dir if project_id else "/workspace/site"

    path_err = _validate_workspace_path(src + "/")
    if path_err:
        return success_response(data={"error": path_err})

    # Build-style site: source_dir = the source-code workspace directory (that is what gets mirrored into the project, not the dist output)
    source_dir = (body.source_dir or "").strip().rstrip("/")
    if source_dir:
        src_err = _validate_workspace_path(source_dir + "/")
        if src_err:
            return success_response(data={"error": f"source_dir 非法: {src_err}"})
        if source_dir == src:
            # source dir == output dir = the caller is publishing source as the build output —
            # error out explicitly; silently degrading would publish unbuilt source as the live site (the page is simply broken).
            return success_response(data={"error": (
                "src_dir 与 source_dir 不能是同一个目录：src_dir 必须指向构建产物"
                "（先 npm run build，产物在 /workspace/.site-dist/ 下，见 init 脚本输出），"
                "source_dir 指向源码工程目录。请构建后带两个不同的目录重试。"
            )})

    # Target site: explicit site_id > the project's already-associated live site (edit / new version) > create new
    target_site_id = (body.site_id or "").strip()
    if not target_site_id and project_id:
        target_site_id = _resolve_target_site_id(project_id, user_id)

    _sess = resolve_sandbox_session(None, body.chat_id or None)

    from core.db.engine import SessionLocal
    from core.infra.exceptions import AppException
    from core.services.site_service import SiteService

    # 1) Pack inside the sandbox + fetch + safe unpack. A build-style publish (source_dir
    #    non-empty) must pack two mutually independent read-only directories (dist output +
    #    source workspace); doing them concurrently saves a full serial round of tar/download
    #    wait (publishing is a model tool call — the user is waiting).
    #    dist/.vite should not exist in the source directory anyway (outDir is in the /workspace
    #    scratch area); the exclusion is just a safety net. Build traces like lockfiles are kept
    #    (editing sessions need them to reproduce dependencies).
    if source_dir:
        (files, err), (src_files, src_err) = await asyncio.gather(
            _pack_and_fetch_dir(src, _sess, user_id),
            _pack_and_fetch_dir(
                source_dir, _sess, user_id,
                extra_excludes=("dist", ".vite", "*.log"),
            ),
        )
    else:
        files, err = await _pack_and_fetch_dir(src, _sess, user_id)
        src_files, src_err = None, None
    if err:
        return success_response(data={"error": f"站点{err}"})
    assert files is not None

    # Block "publishing an unbuilt source workspace as the site" (typical: a build-style editing
    # session forgot to pass src_dir and src fell back to the project folder) — the published
    # live page would simply be broken (index.html referencing /src/main.jsx).
    if not source_dir and SiteService.looks_like_source_tree(p for p, _ in files):
        return success_response(data={"error": (
            f"目录 {src} 是一个未构建的源码工程（含 package.json/src/），"
            "不能直接发布。请先在工程目录 npm run build，再调用 "
            "publish_site(src_dir='<构建产物目录>', source_dir='<源码工程目录>')。"
        )})

    db = SessionLocal()
    try:
        site = SiteService(db).publish(
            user_id=user_id,
            files=files,
            title=body.title,
            slug=body.slug,
            site_id=target_site_id,
            chat_id=body.chat_id or None,
            visibility=body.visibility,
            description=body.description,
            team_id=(body.team_id or None),
            build_info=(
                {"kind": "build", "published_from": src, "source_dir": source_dir}
                if source_dir
                else None
            ),
        )
        # Key point (backend creates the workspace itself, no reliance on frontend/agent): after
        # publishing, ensure the site has a source workspace (a new one is named after the site
        # title), then mirror content into the project folder → the user sees the source in the
        # project and editing sessions can rehydrate it. What gets mirrored depends on the site type:
        #   - build-style (source_dir non-empty): mirror the **source workspace** (dist output only goes to site storage);
        #   - static site: mirror the published files (status quo); but if the project already has
        #     a package.json (build-style workspace) while this publish set doesn't → treat as a
        #     "build-style publish that forgot source_dir" and skip the mirror, so dist output
        #     doesn't overwrite the project's source.
        # dist content is already written to site storage and only source gets mirrored afterwards
        # — free it early (cap ~30MB; combined with src_files there would be a doubled peak memory).
        if source_dir:
            del files
        effective_project_id = _ensure_project_for_site(site.site_id, user_id, site.title)
        mirrored_from = ""
        mirror_note = ""
        if effective_project_id:
            if source_dir:
                if src_files:
                    _mirror_files_to_project_folder(
                        effective_project_id, user_id, src_files
                    )
                    mirrored_from = source_dir
                else:
                    mirror_note = f"（注意：源码目录打包失败，未镜像进项目：{src_err}）"
                    logger.warning(
                        "[internal-sites] source mirror failed: %s", src_err
                    )
            elif _project_root_has_package_json(effective_project_id, user_id):
                # Build-style project + no source_dir passed: never mirror (replace semantics
                # would wipe out the project's source workspace with the published content).
                # Don't inspect the publish file set — when dist happens to contain a
                # package.json (e.g. copied in via public/), a file-set check would wrongly allow it.
                mirror_note = (
                    "（本次发布内容未镜像回项目：项目是构建型源码工程。"
                    "下次发布请带 source_dir 参数指向源码目录，源码才会同步进项目。）"
                )
            else:
                _mirror_files_to_project_folder(effective_project_id, user_id, files)
                mirrored_from = src
        payload = {
            "ok": True,
            "site_id": site.site_id,
            "slug": site.slug,
            "url": f"/site/{site.slug}/",
            "title": site.title,
            "visibility": site.visibility,
            "version": site.current_version,
            "file_count": site.file_count,
            "total_size_bytes": site.total_size_bytes,
            "packed_dir": src,
            "mirrored_from": mirrored_from,
            "note": (
                f"站点已发布（打包目录：{src}，请确认这正是你改动的那份代码所在目录）。"
                "请把访问地址（url 字段，站内相对链接，形如 "
                "/site/<slug>/）以 markdown 链接形式告诉用户；用户也可在"
                "「实验室 → 站点」里管理它，或点站点卡片上的「编辑」按钮，通过对话继续修改这个站点。"
                f"后续要在本会话里继续改这个站点：改完文件后再调 publish_site 并带 site_id='{site.site_id}'。"
                + mirror_note
            ),
        }
        return success_response(data=payload)
    except AppException as exc:
        return success_response(data={"error": exc.message})
    except Exception as exc:  # noqa: BLE001 — model-facing, errors must be readable
        logger.exception("internal site publish failed")
        return success_response(data={"error": f"发布站点失败: {exc}"})
    finally:
        db.close()
