"""Site hosting service — publishing / hosted file retrieval / management for chat-built sites.

Storage layout (storage backend single-file semantics, works for both local/oss):

    sites/<site_id>/v<version>/<relpath>

Version directories are immutable: publishing a new version = write the full
file set to v<n+1> + switch ``current_version``; the hosted URL
``/site/<slug>/`` updates in place; the historical version list is recorded in
``metadata.versions``.
"""

from __future__ import annotations

import logging
import mimetypes
import posixpath
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.db.models import Site
from core.db.repository import SiteRepository
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services.site_access_policy import (
    can_view_site,
    resolve_site_scope,
    site_scope_write_fields,
)
from core.storage import get_storage
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Quotas ───────────────────────────────────────────────────────
MAX_SITE_FILES = 300
MAX_SITE_TOTAL_BYTES = 30 * 1024 * 1024  # 30MB / site
MAX_SITE_FILE_BYTES = 10 * 1024 * 1024  # 10MB / file
MAX_SITES_PER_USER = 50
KEEP_VERSIONS = (
    3  # number of historical versions kept after publishing a new one in local mode (incl. current)
)

# Site-level KV / form-collection quotas (a minimal subset benchmarked against D1/R2)
MAX_KV_KEYS_PER_SITE = 200
MAX_KV_VALUE_BYTES = 4 * 1024
MAX_SUBMISSIONS_PER_SITE = 5000
MAX_SUBMISSION_BYTES = 8 * 1024
KV_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
FORM_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Reserved in-site path prefix: /site/<slug>/__api/** is the dynamic API; publishing files under the same name is not allowed
RESERVED_PATH_PREFIX = "__api"

# slug: 3-50 lowercase letters/digits/hyphens; first and last must be alphanumeric
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
# Avoid existing top-level paths of nginx / frontend / backend (slugs appear
# under /site/<slug>/, so in theory there's no conflict; this list guards
# against a future move of sites to the root path + avoids misleading addresses)
RESERVED_SLUGS = {
    "api",
    "assets",
    "admin",
    "config",
    "docs",
    "files",
    "gateway",
    "health",
    "home",
    "login",
    "logout",
    "mock-sso",
    "openapi",
    "redoc",
    "register",
    "share",
    "site",
    "sites",
    "static",
    "www",
}

# Fill in gaps in the default mimetypes table (/etc/mime.types inside the container may be incomplete)
_EXTRA_MIME = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".md": "text/markdown",
    ".webmanifest": "application/manifest+json",
}
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = {"application/json", "application/javascript", "image/svg+xml"}


def guess_site_mime(path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    mime = _EXTRA_MIME.get(ext)
    if not mime:
        mime, _ = mimetypes.guess_type(path)
    mime = mime or "application/octet-stream"
    if mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIME_EXACT:
        return f"{mime}; charset=utf-8"
    return mime


def normalize_rel_path(path: str) -> Optional[str]:
    """Normalize an in-site relative path; returns None on escape (../, absolute path, empty segment)."""
    raw = (path or "").strip().lstrip("/")
    if not raw:
        return None
    norm = posixpath.normpath(raw)
    if norm.startswith("../") or norm == ".." or norm.startswith("/") or norm == ".":
        return None
    # Reject hidden backslashes / control characters (Windows-style paths or injection)
    if "\\" in norm or any(ord(c) < 0x20 for c in norm):
        return None
    return norm


class SiteService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = SiteRepository(db)

    # ── Publishing ───────────────────────────────────────────────

    @staticmethod
    def looks_like_source_tree(names) -> bool:
        """Whether the file set looks like an unbuilt frontend source project (cannot be published directly as a static site).

        The criteria for build-type projects are owned by this service (same
        source as publish's build_info docstring): has package.json plus an
        src/ layout or a vite config. When new template shapes are added
        (Next/Astro...), extend here — the publish API layer holds none of this
        knowledge.
        """
        names = set(names)
        return "package.json" in names and (
            any(n.startswith("src/") for n in names)
            or any(n.startswith("vite.config.") for n in names)
        )

    def publish(
        self,
        *,
        user_id: str,
        files: List[Tuple[str, bytes]],
        title: str,
        slug: str = "",
        site_id: str = "",
        chat_id: Optional[str] = None,
        visibility: str = "public",
        description: str = "",
        scope_id: Optional[str] = None,
        project_id: Optional[str] = None,
        build_info: Optional[dict] = None,
    ) -> Site:
        """Publish a site: empty ``site_id`` creates a new one, otherwise publishes a new version on the existing site.

        ``project_id`` is only used at creation time to back-fill the site's
        associated source project (a personal project), making the site
        "re-editable"; publishing a new version on an existing site does not
        touch the existing association.

        ``build_info`` is metadata for build-type sites (React/Vite, etc.), e.g.
        {kind, published_from, source_dir}, written into ``extra_data["build"]``
        for observability / frontend badge purposes only — functional detection
        always relies on whether the project root has a package.json.

        ``files`` is a list of (in-site relative path, content), extracted by
        the caller (the publish_site tool) from the sandbox tar package with
        preliminary safety filtering; here we normalize again + enforce quotas
        as a backstop.
        """
        title = (title or "").strip()
        if not title:
            raise BadRequestError("站点标题不能为空")
        resolved_scope_id = resolve_site_scope(self.db, user_id, visibility, scope_id)

        cleaned = self._validate_files(files)
        entry_file = self._pick_entry_file(cleaned)

        # chat_id is for provenance display only; what the caller passes may not
        # be a session in chat_sessions (an automation run / batch item /
        # subagent sandbox session id), so if not found, set it to None to
        # prevent a foreign-key violation from taking down the whole publish.
        if chat_id:
            from core.db.models import ChatSession

            exists = (
                self.db.query(ChatSession.chat_id).filter(ChatSession.chat_id == chat_id).first()
            )
            if not exists:
                chat_id = None

        if site_id:
            site = self.repo.get_by_id(site_id)
            if not site:
                raise ResourceNotFoundError("site", site_id)
            if site.user_id != user_id:
                raise BadRequestError("无权更新该站点（不属于当前用户）")
            return self._publish_new_version(
                site,
                cleaned,
                title=title,
                entry_file=entry_file,
                visibility=visibility,
                description=description,
                scope_id=resolved_scope_id,
                build_info=build_info,
            )

        _, total = self.repo.list_by_user(user_id, page=1, page_size=1)
        if total >= MAX_SITES_PER_USER:
            raise BadRequestError(
                f"站点数量已达上限（{MAX_SITES_PER_USER} 个），请先删除不用的站点"
            )

        final_slug = self._resolve_slug(slug)
        new_id = f"site_{uuid.uuid4().hex[:16]}"
        version = 1
        total_size = self._write_version_files(new_id, version, cleaned)
        return self.repo.create(
            {
                "site_id": new_id,
                "slug": final_slug,
                "user_id": user_id,
                "chat_id": chat_id,
                **site_scope_write_fields(resolved_scope_id),
                "project_id": (project_id or None),
                "title": title,
                "description": description or None,
                "visibility": visibility,
                "entry_file": entry_file,
                "current_version": version,
                "file_count": len(cleaned),
                "total_size_bytes": total_size,
                "extra_data": {
                    "versions": [self._version_meta(version, cleaned, total_size)],
                    **({"build": build_info} if build_info else {}),
                },
            }
        )

    def _publish_new_version(
        self,
        site: Site,
        cleaned: List[Tuple[str, bytes]],
        *,
        title: str,
        entry_file: str,
        visibility: str,
        description: str,
        scope_id: Optional[str] = None,
        build_info: Optional[dict] = None,
    ) -> Site:
        meta = dict(site.extra_data or {})
        if build_info:
            meta["build"] = build_info
        versions = list(meta.get("versions") or [])
        # New version number = historical max + 1 (after a rollback, current_version may be lower than the historical max)
        max_ver = max(
            [int(site.current_version or 1)] + [int(v.get("version") or 0) for v in versions]
        )
        version = max_ver + 1
        total_size = self._write_version_files(site.site_id, version, cleaned)

        versions.append(self._version_meta(version, cleaned, total_size))
        meta["versions"] = versions[-20:]  # keep only the 20 most recent records

        updated = self.repo.update(
            site.site_id,
            {
                "title": title or site.title,
                "description": description or site.description,
                "visibility": visibility or site.visibility,
                **site_scope_write_fields(scope_id),
                "entry_file": entry_file,
                "current_version": version,
                "file_count": len(cleaned),
                "total_size_bytes": total_size,
                "extra_data": meta,
            },
        )
        self._prune_old_versions(site.site_id, version)
        return updated

    @staticmethod
    def _version_meta(
        version: int, cleaned: List[Tuple[str, bytes]], total_size: int
    ) -> Dict[str, Any]:
        return {
            "version": version,
            "file_count": len(cleaned),
            "total_size_bytes": total_size,
            "created_at": datetime.utcnow().isoformat(),
        }

    def _validate_files(self, files: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes]]:
        if not files:
            raise BadRequestError("站点内容为空（目录里没有可发布的文件）")
        if len(files) > MAX_SITE_FILES:
            raise BadRequestError(f"站点文件数超限：{len(files)} > {MAX_SITE_FILES}")
        cleaned: List[Tuple[str, bytes]] = []
        seen: set[str] = set()
        total = 0
        for raw_path, content in files:
            norm = normalize_rel_path(raw_path)
            if norm is None:
                raise BadRequestError(f"非法文件路径：{raw_path}")
            if norm == RESERVED_PATH_PREFIX or norm.startswith(RESERVED_PATH_PREFIX + "/"):
                raise BadRequestError(
                    f"路径前缀 {RESERVED_PATH_PREFIX}/ 是站点动态接口保留区，不能发布同名文件：{norm}"
                )
            if norm in seen:
                continue
            if len(content) > MAX_SITE_FILE_BYTES:
                raise BadRequestError(
                    f"单文件超限：{norm}（{len(content)} bytes > {MAX_SITE_FILE_BYTES}）"
                )
            total += len(content)
            if total > MAX_SITE_TOTAL_BYTES:
                raise BadRequestError(
                    f"站点总大小超限（> {MAX_SITE_TOTAL_BYTES // (1024 * 1024)}MB）"
                )
            seen.add(norm)
            cleaned.append((norm, content))
        return cleaned

    @staticmethod
    def _pick_entry_file(cleaned: List[Tuple[str, bytes]]) -> str:
        paths = {p for p, _ in cleaned}
        if "index.html" in paths:
            return "index.html"
        root_htmls = sorted(p for p in paths if "/" not in p and p.endswith((".html", ".htm")))
        if len(root_htmls) == 1:
            return root_htmls[0]
        raise BadRequestError("站点根目录必须有 index.html（或唯一的一个 .html 文件作为入口）")

    def _resolve_slug(self, slug: str) -> str:
        slug = (slug or "").strip().lower()
        if slug:
            if not SLUG_RE.match(slug):
                raise BadRequestError("slug 仅支持 3-50 位小写字母/数字/连字符，且首尾为字母数字")
            if slug in RESERVED_SLUGS:
                raise BadRequestError(f"slug '{slug}' 是保留字，请换一个")
            if self.repo.get_by_slug(slug):
                raise BadRequestError(f"slug '{slug}' 已被占用，请换一个")
            return slug
        # Auto-generate: s-<8 random chars>, retry on collision
        for _ in range(5):
            candidate = f"s-{uuid.uuid4().hex[:8]}"
            if not self.repo.get_by_slug(candidate):
                return candidate
        raise BadRequestError("slug 生成失败，请重试")

    def _write_version_files(
        self, site_id: str, version: int, cleaned: List[Tuple[str, bytes]]
    ) -> int:
        storage = get_storage()
        total = 0
        for rel_path, content in cleaned:
            key = f"sites/{site_id}/v{version}/{rel_path}"
            storage.upload_bytes(content, key)
            total += len(content)
        return total

    def _prune_old_versions(self, site_id: str, current_version: int) -> None:
        """Physically clean up version directories that are too old in local mode; skip in oss mode (kept, cost negligible)."""
        import os
        import shutil

        if (os.getenv("STORAGE_TYPE", "local").lower()) != "local":
            return
        base = os.getenv("STORAGE_PATH", "./storage")
        site_dir = os.path.join(base, "sites", site_id)
        if not os.path.isdir(site_dir):
            return
        cutoff = current_version - KEEP_VERSIONS
        try:
            for name in os.listdir(site_dir):
                if not name.startswith("v"):
                    continue
                try:
                    ver = int(name[1:])
                except ValueError:
                    continue
                if ver <= cutoff:
                    shutil.rmtree(os.path.join(site_dir, name), ignore_errors=True)
        except OSError as exc:  # a cleanup failure does not affect publishing
            logger.warning("site %s 历史版本清理失败: %s", site_id, exc)

    # ── Hosted file retrieval ────────────────────────────────────

    def resolve_site_file(self, site: Site, path: str) -> Optional[Tuple[bytes, str]]:
        """Fetch a site file by the requested path; returns (bytes, content_type), or None if not found.

        Fallback order: exact path → directory index.html (``foo/`` or
        extensionless ``foo``) → SPA fallback to the entry file
        (extensionless paths only).
        """
        entry = site.entry_file or "index.html"
        raw = (path or "").strip()
        candidates: List[str] = []

        if not raw or raw == "/":
            candidates.append(entry)
        else:
            norm = normalize_rel_path(raw)
            if norm is None:
                return None
            if raw.endswith("/"):
                candidates.append(f"{norm}/index.html")
            else:
                candidates.append(norm)
                if "." not in posixpath.basename(norm):
                    candidates.append(f"{norm}/index.html")
                    candidates.append(entry)  # SPA frontend-routing fallback

        storage = get_storage()
        prefix = f"sites/{site.site_id}/v{site.current_version}"
        for cand in candidates:
            try:
                content = storage.download_bytes(f"{prefix}/{cand}")
            except Exception:
                continue
            return content, guess_site_mime(cand)
        return None

    # ── Management ───────────────────────────────────────────────

    def list_sites(
        self, user_id: str, page: int = 1, page_size: int = 50
    ) -> Tuple[List[Site], int]:
        return self.repo.list_by_user(user_id, page, page_size)

    def get_owned(self, site_id: str, user_id: str) -> Site:
        site = self.repo.get_by_id(site_id)
        if not site or site.user_id != user_id:
            raise ResourceNotFoundError("site", site_id)
        return site

    def update_site(
        self,
        site_id: str,
        user_id: str,
        *,
        title: Optional[str] = None,
        visibility: Optional[str] = None,
        slug: Optional[str] = None,
        description: Optional[str] = None,
        scope_id: Optional[str] = None,
    ) -> Site:
        site = self.get_owned(site_id, user_id)
        data: Dict[str, Any] = {}
        if title is not None:
            title = title.strip()
            if not title:
                raise BadRequestError("站点标题不能为空")
            data["title"] = title
        if visibility is not None:
            resolved_scope_id = resolve_site_scope(self.db, user_id, visibility, scope_id)
            data["visibility"] = visibility
            data.update(site_scope_write_fields(resolved_scope_id))
        if description is not None:
            data["description"] = description or None
        if slug is not None and slug != site.slug:
            data["slug"] = self._resolve_slug(slug)
        if not data:
            return site
        return self.repo.update(site_id, data)

    def rollback(self, site_id: str, user_id: str, version: int) -> Site:
        """Switch the live version in place to a historical one (version directories are immutable; flipping the pointer is the rollback)."""
        site = self.get_owned(site_id, user_id)
        version = int(version)
        if version == site.current_version:
            raise BadRequestError(f"v{version} 已是当前线上版本")
        versions = {
            int(v.get("version") or 0) for v in (site.extra_data or {}).get("versions") or []
        }
        if version not in versions:
            raise BadRequestError(f"版本 v{version} 不存在")
        # The target version's files may have been removed by the local cleanup policy — first confirm the entry file still exists
        storage = get_storage()
        entry = site.entry_file or "index.html"
        try:
            storage.download_bytes(f"sites/{site.site_id}/v{version}/{entry}")
        except Exception:
            raise BadRequestError(f"版本 v{version} 的文件已被清理，无法回滚")
        meta = dict(site.extra_data or {})
        meta["last_rollback"] = {
            "from": site.current_version,
            "to": version,
            "at": datetime.utcnow().isoformat(),
        }
        return self.repo.update(
            site.site_id,
            {
                "current_version": version,
                "extra_data": meta,
            },
        )

    # ── View authorization (shared by the hosting route & site API) ─

    def authorize_view(self, site: Site, viewer_user_id: Optional[str]) -> bool:
        """Decide whether the viewer may access the site under this edition's policy."""
        return can_view_site(self.db, site, viewer_user_id)

    # ── Site-level KV (a minimal subset benchmarked against D1) ──

    @staticmethod
    def _check_kv_key(key: str) -> None:
        if not KV_KEY_RE.match(key or ""):
            raise BadRequestError("KV key 仅支持 1-64 位字母/数字/_.:-")

    def kv_get(self, site: Site, key: str) -> Optional[str]:
        self._check_kv_key(key)
        row = self.repo.kv_get(site.site_id, key)
        return row.v if row else None

    def kv_set(self, site: Site, key: str, value: str) -> None:
        self._check_kv_key(key)
        raw = value if isinstance(value, str) else str(value)
        if len(raw.encode("utf-8")) > MAX_KV_VALUE_BYTES:
            raise BadRequestError(f"KV value 超过 {MAX_KV_VALUE_BYTES} 字节上限")
        if (
            self.repo.kv_get(site.site_id, key) is None
            and self.repo.kv_count(site.site_id) >= MAX_KV_KEYS_PER_SITE
        ):
            raise BadRequestError(f"站点 KV 键数已达上限（{MAX_KV_KEYS_PER_SITE}）")
        self.repo.kv_set(site.site_id, key, raw)

    def kv_delete(self, site: Site, key: str) -> bool:
        self._check_kv_key(key)
        return self.repo.kv_delete(site.site_id, key)

    # ── Form collection (export lands as an artifact) ────────────

    def submit_form(
        self,
        site: Site,
        form_key: str,
        payload: Dict[str, Any],
        client_ip: Optional[str] = None,
    ) -> str:
        if not FORM_KEY_RE.match(form_key or ""):
            raise BadRequestError("form_key 仅支持 1-64 位字母/数字/_-")
        if not isinstance(payload, dict) or not payload:
            raise BadRequestError("表单内容必须是非空 JSON 对象")
        import json as _json

        if len(_json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_SUBMISSION_BYTES:
            raise BadRequestError(f"单条表单数据超过 {MAX_SUBMISSION_BYTES} 字节上限")
        if self.repo.submission_count(site.site_id) >= MAX_SUBMISSIONS_PER_SITE:
            raise BadRequestError("站点表单数据量已达上限，请站主导出后清空")
        row = self.repo.submission_add(site.site_id, form_key, payload, client_ip)
        return row.id

    def export_submissions_to_artifact(self, site_id: str, user_id: str) -> Dict[str, Any]:
        """Export all form submissions as a CSV artifact (persisted; visible and downloadable in "My Space")."""
        site = self.get_owned(site_id, user_id)
        rows, total = self.repo.submission_list(
            site.site_id, page=1, page_size=MAX_SUBMISSIONS_PER_SITE
        )
        if not rows:
            raise BadRequestError("该站点还没有表单数据")

        import csv
        import io
        import json as _json

        field_names: List[str] = []
        for r in rows:
            for k in (r.payload or {}).keys():
                if k not in field_names:
                    field_names.append(k)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["提交时间", "表单", *field_names])
        for r in reversed(rows):  # export in chronological order
            payload = r.payload or {}
            writer.writerow(
                [
                    r.created_at.isoformat() if r.created_at else "",
                    r.form_key,
                    *[
                        (
                            _json.dumps(payload.get(k), ensure_ascii=False)
                            if isinstance(payload.get(k), (dict, list))
                            else ("" if payload.get(k) is None else str(payload.get(k)))
                        )
                        for k in field_names
                    ],
                ]
            )
        content = buf.getvalue().encode(
            "utf-8-sig"
        )  # BOM: opens directly in Excel without mojibake

        from core.artifacts.store import save_artifact_bytes
        from core.services.artifact_service import ArtifactService

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{site.title}_表单数据_{ts}.csv"
        item = save_artifact_bytes(
            content=content,
            name=filename,
            mime_type="text/csv",
            extension="csv",
            metadata={"source": "site_submissions_export", "site_id": site.site_id},
        )
        artifact = ArtifactService(self.db).create_artifact(
            user_id=user_id,
            artifact_type="document",
            title=filename,
            filename=filename,
            size_bytes=len(content),
            mime_type="text/csv",
            storage_key=item["storage_key"],
        )
        return {
            "artifact_id": artifact["artifact_id"],
            "filename": filename,
            "rows": total,
            "download_url": f"/files/{artifact['artifact_id']}",
        }

    def delete_site(self, site_id: str, user_id: str) -> None:
        site = self.get_owned(site_id, user_id)
        self.repo.soft_delete(site.site_id)
        # Physically delete files in local mode; keep them in oss mode (the soft delete has already freed the slug)
        import os
        import shutil

        if (os.getenv("STORAGE_TYPE", "local").lower()) == "local":
            base = os.getenv("STORAGE_PATH", "./storage")
            shutil.rmtree(os.path.join(base, "sites", site.site_id), ignore_errors=True)
