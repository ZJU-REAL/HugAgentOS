"""Site management, access, KV and form operations."""

from __future__ import annotations
from core.infra.time import utc_now

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.db.models import Site
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services.site_access_policy import (
    can_view_site, site_listing_predicate, site_management_permission,
    resolve_site_scope, site_scope_write_fields,
)
from core.services.site_password import hash_access_password, verify_access_token
from core.storage import get_storage

MAX_KV_KEYS_PER_SITE = 200
MAX_KV_VALUE_BYTES = 4 * 1024
MAX_SUBMISSIONS_PER_SITE = 5000
MAX_SUBMISSION_BYTES = 8 * 1024
KV_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
FORM_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SiteManagementMixin:
    # ── Management ───────────────────────────────────────────────

    def list_sites(
        self, user_id: str, page: int = 1, page_size: int = 50
    ) -> Tuple[List[Site], int]:
        query = self.db.query(Site).filter(Site.deleted_at.is_(None), site_listing_predicate(user_id))
        total = query.count()
        return query.order_by(Site.updated_at.desc()).offset((page - 1) * page_size).limit(page_size).all(), total

    def get_owned(
        self, site_id: str, user_id: str, *, required: str = "admin", for_update: bool = False
    ) -> Site:
        """取站点并校验权限。`for_update` 只由改写 sites 行本身的路径开启——权限级别与行锁
        是两件正交的事，读路径拿排他锁会让并发打开站点面板的请求互相阻塞。"""
        from fastapi import HTTPException
        site = (self.db.query(Site).filter(
            Site.site_id == site_id, Site.deleted_at.is_(None),
        ).populate_existing().with_for_update().first()
                if for_update else self.repo.get_by_id(site_id))
        level = site_management_permission(self.db, site, user_id) if site else "none"
        if level == "none":
            raise ResourceNotFoundError("site", site_id)
        if {"view": 1, "edit": 2, "admin": 3}[level] < {"view": 1, "edit": 2, "admin": 3}[required]:
            raise HTTPException(403, "当前站点权限不足")
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
        site = self.get_owned(site_id, user_id, for_update=True)
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
        site = self.get_owned(site_id, user_id, for_update=True)
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
            "at": utc_now().isoformat(),
        }
        return self.repo.update(
            site.site_id,
            {
                "current_version": version,
                "extra_data": meta,
            },
        )

    # ── Access password (an extra gate on top of the public link) ──

    def set_access_password(self, site_id: str, user_id: str, password: str) -> Site:
        # 校验与 Argon2 计算（约 50ms）都放在取行锁之前，别让它们撑长锁持有时间。
        password_hash = hash_access_password(password)
        site = self.get_owned(site_id, user_id, for_update=True)
        return self.repo.update(site.site_id, {"access_password_hash": password_hash})

    def clear_access_password(self, site_id: str, user_id: str) -> Site:
        site = self.get_owned(site_id, user_id, for_update=True)
        return self.repo.update(site.site_id, {"access_password_hash": None})

    def authorize_access(
        self, site: Site, viewer_user_id: Optional[str], access_token: Optional[str]
    ) -> bool:
        """站点设了访问密码时，访客需持有效凭据；能管理该站点的人无需解锁。"""
        if not site.access_password_hash:
            return True
        if verify_access_token(site, access_token):
            return True
        return bool(
            viewer_user_id and site_management_permission(self.db, site, viewer_user_id) != "none"
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

    def kv_list(self, site: Site, *, limit: int = 500) -> tuple[list, int]:
        """按 key 升序返回最多 limit 条，外加真实总数（total > len(rows) 即还有更多）。"""
        return self.repo.kv_list(site.site_id, limit=limit), self.repo.kv_count(site.site_id)

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

        ts = utc_now().strftime("%Y%m%d_%H%M%S")
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
        site = self.get_owned(site_id, user_id, for_update=True)
        self.repo.soft_delete(site.site_id)
        # Physically delete files in local mode; keep them in oss mode (the soft delete has already freed the slug)
        import os
        import shutil

        if (os.getenv("STORAGE_TYPE", "local").lower()) == "local":
            base = os.getenv("STORAGE_PATH", "./storage")
            shutil.rmtree(os.path.join(base, "sites", site.site_id), ignore_errors=True)
