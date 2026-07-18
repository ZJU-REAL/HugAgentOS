"""Knowledge base business logic."""

from typing import Optional, Dict, Any, Tuple
from datetime import datetime
import os
import uuid
from sqlalchemy.orm import Session

from core.db.repository import KBRepository, AuditLogRepository, ArtifactRepository
from core.db.models import KBDocument, KBChunk, UserShadow
from core.storage import get_storage
from core.content.kb_processing import vectorise_document_background


# Fixed owner for admin-managed public knowledge bases. Public KB spaces are owned
# by this synthetic system account so that admin endpoints can reuse the ownership-
# scoped KBService methods (upload/delete/update) by passing this user_id.
SYSTEM_KB_OWNER_ID = "system_public_kb"
SYSTEM_KB_OWNER_NAME = "系统公共知识库"

MANAGED_SYNC_KB_NAME = "我的空间同步知识库"
MANAGED_SYNC_KB_TAG = "系统托管"
MANAGED_SYNC_KB_DESCRIPTION = (
    "与AI会话过程中生成的文件，自动同步到此知识库完成索引\n\n"
    "规则说明：\n"
    "1. 该知识库由系统自动维护，并固定置顶展示。\n"
    "2. 该知识库不可编辑、不可删除，也不支持手动上传文档。\n"
    "3. 当“同步文件到知识库”开关开启后，系统会将“我的空间”中的已有文档和图片同步到此知识库。\n"
    "4. 开启后，后续新增到“我的空间”的文档和图片也会自动同步到此知识库。\n"
    "5. 已同步文档支持删除和重新索引。\n"
    "6. 关闭开关后，仅停止后续自动同步，已同步内容不会自动删除。"
)
MANAGED_SYNC_KB_META = {
    "system_managed": True,
    "managed_type": "my_space_sync",
    "pinned": True,
    "editable": False,
    "deletable": False,
    "uploadable": False,
    "tag": MANAGED_SYNC_KB_TAG,
}


class KBService:
    """Service for knowledge base operations."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = KBRepository(db)
        self.audit_repo = AuditLogRepository(db)

    def _has_kb_level(self, space: Any, user_id: str, required: str) -> bool:
        """Unified write-permission decision (public-KB permission assignment): owner/superadmin
        are always admin, authorized users by their level.

        Replaces the historical single ``space.user_id == user_id`` ownership check — letting
        authorized users of a scoped KB (edit can upload, admin can manage) operate through the
        user-mode routes; public-KB management still goes through the admin routes (invoked as
        SYSTEM_KB_OWNER_ID, i.e. the owner identity, equivalent to admin).
        """
        if not space:
            return False
        from core.auth.kb_permissions import has_kb_permission, resolve_local_kb_level
        return has_kb_permission(resolve_local_kb_level(self.db, user_id, space.kb_id), required)

    def _normalize_user_settings(self, user: Optional[UserShadow]) -> Dict[str, Any]:
        return dict(user.extra_data) if user and isinstance(user.extra_data, dict) else {}

    def _get_user(self, user_id: str) -> Optional[UserShadow]:
        return self.db.query(UserShadow).filter(UserShadow.user_id == user_id).first()

    def ensure_system_owner(self) -> str:
        """Ensure the synthetic system owner row exists; return its user_id.

        Public KB spaces are owned by this account so that admin-side management can
        reuse the per-owner KBService methods. The row is created lazily on first use
        (no Alembic migration needed) to satisfy the KBSpace.user_id foreign key.
        """
        existing = self.db.query(UserShadow).filter(
            UserShadow.user_id == SYSTEM_KB_OWNER_ID
        ).first()
        if existing:
            return SYSTEM_KB_OWNER_ID
        owner = UserShadow(
            user_id=SYSTEM_KB_OWNER_ID,
            username=SYSTEM_KB_OWNER_NAME,
            extra_data={"system_account": True, "purpose": "public_kb_owner"},
        )
        self.db.add(owner)
        self.db.commit()
        return SYSTEM_KB_OWNER_ID

    def _is_system_managed_space(self, space: Any) -> bool:
        extra = space.extra_data if isinstance(getattr(space, "extra_data", None), dict) else {}
        return bool(extra.get("system_managed"))

    def _get_system_managed_space(self, user_id: str) -> Optional[Any]:
        for space in self.repo.list_spaces(user_id):
            if self._is_system_managed_space(space):
                return space
        return None

    def _ensure_managed_metadata(self, space: Any) -> Any:
        current = dict(space.extra_data or {}) if isinstance(space.extra_data, dict) else {}
        merged = {**current, **MANAGED_SYNC_KB_META}
        changed = (
            space.name != MANAGED_SYNC_KB_NAME
            or (space.description or "") != MANAGED_SYNC_KB_DESCRIPTION
            or space.visibility != "private"
            or current != merged
        )
        if changed:
            space.name = MANAGED_SYNC_KB_NAME
            space.description = MANAGED_SYNC_KB_DESCRIPTION
            space.visibility = "private"
            space.extra_data = merged
            space.updated_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(space)
        return space

    def _list_all_space_documents(self, kb_id: str) -> list[KBDocument]:
        return (
            self.db.query(KBDocument)
            .filter(
                KBDocument.kb_id == kb_id,
                KBDocument.deleted_at.is_(None),
            )
            .all()
        )

    def _find_managed_document_by_artifact(self, kb_id: str, artifact_id: str) -> Optional[KBDocument]:
        for document in self._list_all_space_documents(kb_id):
            meta = document.extra_data if isinstance(document.extra_data, dict) else {}
            if meta.get("source_artifact_id") == artifact_id:
                return document
        return None

    def ensure_my_space_sync_space(self, user_id: str) -> Tuple[Any, bool]:
        space = self._get_system_managed_space(user_id)
        created = False
        if not space:
            space = self.repo.create_space({
                "kb_id": f"kb_{uuid.uuid4().hex[:16]}",
                "user_id": user_id,
                "name": MANAGED_SYNC_KB_NAME,
                "description": MANAGED_SYNC_KB_DESCRIPTION,
                "visibility": "private",
                "chunk_method": "semantic",
                "extra_data": dict(MANAGED_SYNC_KB_META),
            })
            created = True
            self.audit_repo.create({
                "user_id": user_id,
                "action": "kb.space.system_managed.created",
                "resource_type": "kb_space",
                "resource_id": space.kb_id,
                "status": "success",
            })
        else:
            space = self._ensure_managed_metadata(space)

        return space, created

    def get_my_space_sync_settings(self, user_id: str) -> Dict[str, Any]:
        user = self._get_user(user_id)
        settings = self._normalize_user_settings(user)
        sync_settings = settings.get("my_space_sync_kb", {}) if isinstance(settings.get("my_space_sync_kb"), dict) else {}
        space, _ = self.ensure_my_space_sync_space(user_id)
        return {
            "enabled": bool(sync_settings.get("enabled", False)),
            "kb_id": space.kb_id,
            "kb_name": space.name,
        }

    def _set_my_space_sync_enabled(self, user_id: str, enabled: bool) -> None:
        user = self._get_user(user_id)
        if not user:
            return
        metadata = self._normalize_user_settings(user)
        sync_settings = metadata.get("my_space_sync_kb", {}) if isinstance(metadata.get("my_space_sync_kb"), dict) else {}
        sync_settings["enabled"] = enabled
        sync_settings["updated_at"] = datetime.utcnow().isoformat()
        metadata["my_space_sync_kb"] = sync_settings
        user.extra_data = metadata
        user.updated_at = datetime.utcnow()
        self.db.commit()

    def is_my_space_sync_enabled(self, user_id: str) -> bool:
        return bool(self.get_my_space_sync_settings(user_id).get("enabled"))

    def sync_artifact_to_my_space_kb(self, artifact: Any, file_bytes: Optional[bytes] = None) -> Optional[Dict[str, Any]]:
        if not artifact or not artifact.user_id:
            return None

        user_id = str(artifact.user_id)
        if not self.is_my_space_sync_enabled(user_id):
            return None

        mime_type = artifact.mime_type or "application/octet-stream"
        if not (
            mime_type.startswith("image/")
            or mime_type.startswith("text/")
            or mime_type in {
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
                "text/csv",
                "application/json",
            }
        ):
            return None

        space, _ = self.ensure_my_space_sync_space(user_id)
        existing = self._find_managed_document_by_artifact(space.kb_id, artifact.artifact_id)
        if existing:
            return {
                "document_id": existing.document_id,
                "kb_id": existing.kb_id,
                "title": existing.title,
                "filename": existing.filename,
                "size_bytes": existing.size_bytes,
                "uploaded_at": existing.uploaded_at.isoformat() if existing.uploaded_at else "",
            }

        document = self.repo.create_document({
            "document_id": f"doc_{uuid.uuid4().hex[:16]}",
            "kb_id": space.kb_id,
            "title": artifact.title or artifact.filename,
            "filename": artifact.filename,
            "size_bytes": artifact.size_bytes,
            "mime_type": mime_type,
            "storage_key": artifact.storage_key,
            "storage_url": artifact.storage_url,
            "checksum": None,
            "indexing_status": "processing",
            "extra_data": {
                "source": "my_space_sync",
                "source_artifact_id": artifact.artifact_id,
                "system_managed": True,
            },
        })

        space.document_count = (space.document_count or 0) + 1
        space.total_size_bytes = (space.total_size_bytes or 0) + max(artifact.size_bytes or 0, 0)
        self.db.commit()

        if file_bytes is None:
            storage = get_storage()
            file_bytes = storage.download_bytes(artifact.storage_key)

        vectorise_document_background(
            document_id=document.document_id,
            kb_id=space.kb_id,
            user_id=user_id,
            title=document.title,
            file_bytes=file_bytes,
            mime_type=mime_type,
            chunk_method=space.chunk_method or "semantic",
            db_url=os.getenv("DATABASE_URL", ""),
            indexing_config=None,
        )

        self.audit_repo.create({
            "user_id": user_id,
            "action": "kb.document.synced_from_my_space",
            "resource_type": "kb_document",
            "resource_id": document.document_id,
            "details": {"artifact_id": artifact.artifact_id, "kb_id": space.kb_id},
            "status": "success",
        })

        return {
            "document_id": document.document_id,
            "kb_id": document.kb_id,
            "title": document.title,
            "filename": document.filename,
            "size_bytes": document.size_bytes,
            "uploaded_at": document.uploaded_at.isoformat() if document.uploaded_at else "",
        }

    def sync_all_my_space_artifacts(self, user_id: str) -> Dict[str, int]:
        from core.db.models import Artifact

        artifacts = (
            self.db.query(Artifact)
            .filter(
                Artifact.user_id == user_id,
                Artifact.deleted_at.is_(None),
            )
            .all()
        )
        space, _ = self.ensure_my_space_sync_space(user_id)

        synced = 0
        skipped = 0
        for artifact in artifacts:
            try:
                if self._find_managed_document_by_artifact(space.kb_id, artifact.artifact_id):
                    skipped += 1
                    continue
                if self.sync_artifact_to_my_space_kb(artifact):
                    synced += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        return {"synced": synced, "skipped": skipped}

    def update_my_space_sync_enabled(self, user_id: str, enabled: bool) -> Dict[str, Any]:
        space, _ = self.ensure_my_space_sync_space(user_id)
        self._set_my_space_sync_enabled(user_id, enabled)
        sync_result = {"synced": 0, "skipped": 0}
        if enabled:
            sync_result = self.sync_all_my_space_artifacts(user_id)
        return {
            "enabled": enabled,
            "kb_id": space.kb_id,
            "kb_name": space.name,
            "sync_result": sync_result,
        }

    def delete_space(self, kb_id: str, user_id: str) -> bool:
        """Delete a KB space (soft delete)."""
        space = self.repo.get_space(kb_id)
        if space and self._is_system_managed_space(space):
            self.audit_repo.create({
                "user_id": user_id,
                "action": "kb.space.delete.failed",
                "resource_type": "kb_space",
                "resource_id": kb_id,
                "status": "failed",
                "details": {"reason": "system_managed"},
            })
            return False
        if not self._has_kb_level(space, user_id, "admin"):
            # Audit failed attempt
            self.audit_repo.create({
                "user_id": user_id,
                "action": "kb.space.delete.failed",
                "resource_type": "kb_space",
                "resource_id": kb_id,
                "status": "failed",
                "details": {"reason": "not_found_or_unauthorized"}
            })
            return False

        # Perform soft delete
        space.deleted_at = datetime.utcnow()
        self.db.commit()

        # Audit log
        self.audit_repo.create({
            "user_id": user_id,
            "action": "kb.space.deleted",
            "resource_type": "kb_space",
            "resource_id": kb_id,
            "status": "success"
        })

        return True

    def delete_document(self, document_id: str, user_id: str) -> bool:
        """Delete a KB document (soft delete)."""
        document = self.repo.get_document(document_id)
        if not document:
            return False

        # Check access through space (edit level required to delete documents)
        space = self.repo.get_space(document.kb_id)
        if not self._has_kb_level(space, user_id, "edit"):
            # Audit failed attempt
            self.audit_repo.create({
                "user_id": user_id,
                "action": "kb.document.delete.failed",
                "resource_type": "kb_document",
                "resource_id": document_id,
                "status": "failed",
                "details": {"reason": "not_found_or_unauthorized"}
            })
            return False

        # Perform soft delete
        document.deleted_at = datetime.utcnow()

        # Decrement document count and total size on the KB space
        space.document_count = max((space.document_count or 0) - 1, 0)
        space.total_size_bytes = max((space.total_size_bytes or 0) - (document.size_bytes or 0), 0)

        self.db.commit()

        # Audit log
        self.audit_repo.create({
            "user_id": user_id,
            "action": "kb.document.deleted",
            "resource_type": "kb_document",
            "resource_id": document_id,
            "details": {"kb_id": document.kb_id, "filename": document.filename},
            "status": "success"
        })

        return True

    def create_space(
        self,
        user_id: str,
        name: str,
        description: Optional[str] = None,
        chunk_method: str = "semantic",
        metadata: Optional[Dict[str, Any]] = None,
        visibility: str = "private",
        grant_team_ids: Optional[list] = None,
        granted_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new KB space.

        ``visibility`` defaults to ``private`` so existing user-facing callers keep
        their behaviour; admin-managed public KBs pass ``visibility="public"``.

        ``grant_team_ids``: teams granted visibility right after creating the KB (under the
        default not-visible model, a public KB needs a grant before anyone can see it).
        Creation and granting are unified in this single domain operation, avoiding each route
        stitching a ``bulk_grant`` after create on its own and drifting apart.
        """
        space_data = {
            "kb_id": f"kb_{uuid.uuid4().hex[:16]}",
            "user_id": user_id,
            "name": name,
            "description": description,
            "visibility": visibility,
            "chunk_method": chunk_method,
            "extra_data": metadata or {},
        }

        space = self.repo.create_space(space_data)

        # After creating the KB, grant team visibility as needed (team members inherit view; the creator, as owner, can always manage)
        team_ids = [str(t).strip() for t in (grant_team_ids or []) if str(t).strip()]
        if team_ids:
            from core.db.repository import KBGrantRepository
            KBGrantRepository(self.db).bulk_grant(
                space.kb_id, "local", [("team", tid) for tid in team_ids], "view",
                granted_by=granted_by or user_id,
            )

        # Audit log
        self.audit_repo.create({
            "user_id": user_id,
            "action": "kb.space.created",
            "resource_type": "kb_space",
            "resource_id": space.kb_id,
            "status": "success"
        })

        return {
            "kb_id": space.kb_id,
            "name": space.name,
            "description": space.description,
            "document_count": space.document_count,
            "created_at": space.created_at.isoformat()
        }

    def update_space(
        self,
        kb_id: str,
        user_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update an existing KB space."""
        space = self.repo.get_space(kb_id)
        if space and self._is_system_managed_space(space):
            self.audit_repo.create({
                "user_id": user_id,
                "action": "kb.space.update.failed",
                "resource_type": "kb_space",
                "resource_id": kb_id,
                "status": "failed",
                "details": {"reason": "system_managed"},
            })
            return None
        if not self._has_kb_level(space, user_id, "admin"):
            self.audit_repo.create({
                "user_id": user_id,
                "action": "kb.space.update.failed",
                "resource_type": "kb_space",
                "resource_id": kb_id,
                "status": "failed",
                "details": {"reason": "not_found_or_unauthorized"},
            })
            return None

        update_data: Dict[str, Any] = {}
        if name is not None:
            update_data["name"] = name
        if description is not None:
            update_data["description"] = description

        updated = self.repo.update_space(kb_id, update_data)
        if not updated:
            return None

        self.audit_repo.create({
            "user_id": user_id,
            "action": "kb.space.updated",
            "resource_type": "kb_space",
            "resource_id": kb_id,
            "status": "success",
        })

        return {
            "kb_id": updated.kb_id,
            "name": updated.name,
            "description": updated.description,
            "document_count": updated.document_count,
            "created_at": updated.created_at.isoformat() if updated.created_at else "",
            "updated_at": updated.updated_at.isoformat() if updated.updated_at else "",
        }

    def upload_document(
        self,
        kb_id: str,
        user_id: str,
        title: str,
        filename: str,
        size_bytes: int,
        mime_type: str,
        storage_key: str,
        checksum: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload a document to KB space."""
        # Check ownership
        space = self.repo.get_space(kb_id)
        if space and self._is_system_managed_space(space):
            raise PermissionError("System managed KB space does not allow manual uploads")
        if not self._has_kb_level(space, user_id, "edit"):
            raise PermissionError("Access denied to this KB space")

        document_data = {
            "document_id": f"doc_{uuid.uuid4().hex[:16]}",
            "kb_id": kb_id,
            "title": title,
            "filename": filename,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "storage_key": storage_key,
            "checksum": checksum,
            "indexing_status": "processing",
        }

        document = self.repo.create_document(document_data)

        # Increment document count on the KB space
        space.document_count = (space.document_count or 0) + 1
        space.total_size_bytes = (space.total_size_bytes or 0) + size_bytes
        self.db.commit()

        # Audit log
        self.audit_repo.create({
            "user_id": user_id,
            "action": "kb.document.uploaded",
            "resource_type": "kb_document",
            "resource_id": document.document_id,
            "details": {"kb_id": kb_id, "filename": filename},
            "status": "success"
        })

        return {
            "document_id": document.document_id,
            "kb_id": document.kb_id,
            "title": document.title,
            "filename": document.filename,
            "size_bytes": document.size_bytes,
            "uploaded_at": document.uploaded_at.isoformat()
        }

    def add_artifact_to_space(self, artifact_id: str, user_id: str, kb_id: str) -> Dict[str, Any]:
        """Create a KB document from an existing user artifact."""
        from core.auth.permissions_iface import has_permission, resolve_artifact_access

        artifact_repo = ArtifactRepository(self.db)
        artifact = artifact_repo.get_by_id(artifact_id)
        if not artifact:
            raise ValueError("资源不存在或无权限")
        # owner ∪ team composed-permission single point (owner always allowed; team view+ members can join a personal KB)
        perm = resolve_artifact_access(self.db, user_id, artifact.user_id, artifact.team_id)
        if not has_permission(perm, "view"):
            raise ValueError("资源不存在或无权限")

        space = self.repo.get_space(kb_id)
        if not space:
            raise ValueError("知识库不存在")
        if space.user_id != user_id:
            raise PermissionError("只能加入到你自己的私有知识库")
        if self._is_system_managed_space(space):
            raise PermissionError("系统托管知识库不支持手动加入文件")

        for document in self._list_all_space_documents(kb_id):
            meta = document.extra_data if isinstance(document.extra_data, dict) else {}
            if meta.get("source_artifact_id") == artifact_id:
                return {
                    "document_id": document.document_id,
                    "kb_id": document.kb_id,
                    "title": document.title,
                    "filename": document.filename,
                    "size_bytes": document.size_bytes,
                    "uploaded_at": document.uploaded_at.isoformat() if document.uploaded_at else "",
                    "already_exists": True,
                    "chunk_method": space.chunk_method or "semantic",
                }

        document_data = {
            "document_id": f"doc_{uuid.uuid4().hex[:16]}",
            "kb_id": kb_id,
            "title": artifact.title or artifact.filename or artifact.artifact_id,
            "filename": artifact.filename or artifact.title or artifact.artifact_id,
            "size_bytes": artifact.size_bytes or 0,
            "mime_type": artifact.mime_type or "application/octet-stream",
            "storage_key": artifact.storage_key,
            "checksum": None,
            "indexing_status": "processing",
            "extra_data": {
                "source": "my_space_manual",
                "source_artifact_id": artifact_id,
            },
        }
        document = self.repo.create_document(document_data)

        space.document_count = (space.document_count or 0) + 1
        space.total_size_bytes = (space.total_size_bytes or 0) + (artifact.size_bytes or 0)
        self.db.commit()

        space_extra = space.extra_data if isinstance(space.extra_data, dict) else {}

        self.audit_repo.create({
            "user_id": user_id,
            "action": "kb.document.added_from_my_space",
            "resource_type": "kb_document",
            "resource_id": document.document_id,
            "details": {"kb_id": kb_id, "artifact_id": artifact_id},
            "status": "success",
        })

        return {
            "document_id": document.document_id,
            "kb_id": document.kb_id,
            "title": document.title,
            "filename": document.filename,
            "size_bytes": document.size_bytes,
            "uploaded_at": document.uploaded_at.isoformat() if document.uploaded_at else "",
            "already_exists": False,
            "chunk_method": space.chunk_method or "semantic",
            "indexing_config": space_extra.get("indexing_config"),
        }
