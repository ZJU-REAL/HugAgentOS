"""Bounded UTF-8 artifact editing with permission checks and versioned saves."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import PurePosixPath

from fastapi import HTTPException
from sqlalchemy import update

from core.auth.edition import resolve_artifact_access
from core.db.models import Artifact
from core.infra.time import utc_now
from core.files.edition import artifact_access_metadata
from core.projects.source import reserve_sqlite_writer

MAX_TEXT_BYTES = 2 * 1024 * 1024
TEXT_EXTENSIONS = frozenset(
    "md markdown mdx txt log csv tsv json json5 jsonl ndjson xml yaml yml ini conf "
    "env toml lock py js ts tsx jsx mjs cjs html htm css scss sass less "
    "sh bash zsh fish ps1 bat cmd go rs java kt swift c cpp cc h hpp rb php r "
    "sql graphql gql proto vue svelte astro".split()
)


def decode_text(data: bytes) -> str:
    if len(data) > MAX_TEXT_BYTES:
        raise HTTPException(413, "在线编辑最多支持 2 MiB 文本")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(415, "在线编辑仅支持 UTF-8 文本") from exc
    if any(ord(ch) < 32 and ch not in "\t\r\n" for ch in text):
        raise HTTPException(415, "二进制文件不支持在线编辑")
    return text


class ArtifactTextService:
    def __init__(self, db):
        self.db = db

    def _authorized(self, file_id, actor, *, write=False):
        if write:
            reserve_sqlite_writer(self.db)
        query = (
            self.db.query(Artifact)
            .filter(Artifact.artifact_id == file_id, Artifact.deleted_at.is_(None))
            .populate_existing()
        )
        artifact = (query.with_for_update() if write else query).first()
        metadata = artifact_access_metadata(artifact) if artifact else {}
        level = (
            resolve_artifact_access(
                self.db, actor, metadata.get("owner_id"), metadata.get("scope_id")
            )
            if artifact
            else "none"
        )
        if level == "none":
            raise HTTPException(404, "文件不存在或无权访问")
        if write and level not in ("edit", "admin"):
            raise HTTPException(403, "当前文件只读")
        name = PurePosixPath(artifact.filename).name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        mime = (artifact.mime_type or "").lower()
        if mime.startswith(("image/", "audio/", "video/")) or (
            ext not in TEXT_EXTENSIONS and not mime.startswith("text/")
        ):
            raise HTTPException(415, "此格式不支持在线编辑")
        if artifact.size_bytes > MAX_TEXT_BYTES:
            raise HTTPException(413, "在线编辑最多支持 2 MiB 文本")
        return artifact, level

    @staticmethod
    def _revision(artifact, data):
        # Include storage generation to reject changes even when bytes match again.
        stamp = str(artifact.updated_at or artifact.created_at)
        return hashlib.sha256(
            artifact.storage_key.encode() + b"\0" + stamp.encode() + b"\0" + data
        ).hexdigest()

    def _document(self, artifact, level, data):
        return {
            "file_id": artifact.artifact_id,
            "name": artifact.filename,
            "content": decode_text(data),
            "revision": self._revision(artifact, data),
            "size": len(data),
            "can_edit": level in ("edit", "admin"),
        }

    def read(self, file_id, actor):
        from core.files.storage import get_storage

        artifact, level = self._authorized(file_id, actor)
        try:
            data = get_storage().download_bytes(artifact.storage_key)
        except Exception as exc:
            raise HTTPException(503, "文件读取失败，请重试") from exc
        return self._document(artifact, level, data)

    def save(self, file_id, actor, content, revision):
        from core.files.storage import get_storage

        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPException(415, "在线编辑仅支持 UTF-8 文本") from exc
        decode_text(data)
        artifact, level = self._authorized(file_id, actor, write=True)
        storage = get_storage()
        try:
            previous = storage.download_bytes(artifact.storage_key)
        except Exception as exc:
            raise HTTPException(503, "文件读取失败，请重试") from exc
        decode_text(previous)
        if self._revision(artifact, previous) != revision:
            raise HTTPException(409, "文件已被修改，请保留草稿并重新打开文件后合并")
        old_key, old_time = artifact.storage_key, artifact.updated_at
        key = f"file_versions/{file_id}/{uuid.uuid4().hex}"
        try:
            url = storage.upload_bytes(data, key)
        except Exception as exc:
            raise HTTPException(503, "保存失败，请重试") from exc
        # CAS also protects against writers that don't take row locks.
        changed = self.db.execute(
            update(Artifact)
            .where(
                Artifact.artifact_id == file_id,
                Artifact.deleted_at.is_(None),
                Artifact.storage_key == old_key,
                Artifact.updated_at == old_time,
            )
            .values(
                storage_key=key,
                storage_url=url,
                size_bytes=len(data),
                parsed_text=None,
                summary=None,
                parsed_at=None,
                parse_error=None,
                updated_at=utc_now(),
            ),
            execution_options={"synchronize_session": False},
        )
        if changed.rowcount != 1:
            self.db.rollback()
            try:
                storage.delete(key)
            except Exception as fallback_error:
                from core.infra.failures import record_best_effort_failure
                record_best_effort_failure('core.files.artifacts.text:save', fallback_error)
            raise HTTPException(409, "文件已被修改，请保留草稿并重新打开文件后合并")
        self.db.commit()
        self.db.refresh(artifact)
        return self._document(artifact, level, data)
