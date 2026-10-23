"""Knowledge base business logic."""
from core.infra.time import utc_now

import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from features.knowledge.processing import vectorise_document_background
from features.knowledge.models import KBChunk, KBDocument
from core.db.models import UserShadow
from features.knowledge.wiki.config import index_modes_of
from core.db.repository import ArtifactRepository, AuditLogRepository
from features.knowledge.repository import KBRepository
from core.files.edition import can_access_artifact
from core.files.storage import get_storage
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Fixed owner for admin-managed public knowledge bases. Public KB spaces are owned
# by this synthetic system account so that admin endpoints can reuse the ownership-
# scoped KBService methods (upload/delete/update) by passing this user_id.
SYSTEM_KB_OWNER_ID = "system_public_kb"
SYSTEM_KB_OWNER_NAME = "系统公共知识库"

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
        from features.knowledge.edition import has_kb_permission, resolve_local_kb_level

        return has_kb_permission(resolve_local_kb_level(self.db, user_id, space.kb_id), required)



    def ensure_system_owner(self) -> str:
        """Ensure the synthetic system owner row exists; return its user_id.

        Public KB spaces are owned by this account so that admin-side management can
        reuse the per-owner KBService methods. The row is created lazily on first use
        (no Alembic migration needed) to satisfy the KBSpace.user_id foreign key.
        """
        existing = (
            self.db.query(UserShadow).filter(UserShadow.user_id == SYSTEM_KB_OWNER_ID).first()
        )
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



    def _list_all_space_documents(self, kb_id: str) -> list[KBDocument]:
        return (
            self.db.query(KBDocument)
            .filter(
                KBDocument.kb_id == kb_id,
                KBDocument.deleted_at.is_(None),
            )
            .all()
        )









    def delete_space(self, kb_id: str, user_id: str) -> bool:
        """Delete a KB space (soft delete)."""
        space = self.repo.get_space(kb_id)
        if space and self._is_system_managed_space(space):
            self.audit_repo.create(
                {
                    "user_id": user_id,
                    "action": "kb.space.delete.failed",
                    "resource_type": "kb_space",
                    "resource_id": kb_id,
                    "status": "failed",
                    "details": {"reason": "system_managed"},
                }
            )
            return False
        if not self._has_kb_level(space, user_id, "admin"):
            # Audit failed attempt
            self.audit_repo.create(
                {
                    "user_id": user_id,
                    "action": "kb.space.delete.failed",
                    "resource_type": "kb_space",
                    "resource_id": kb_id,
                    "status": "failed",
                    "details": {"reason": "not_found_or_unauthorized"},
                }
            )
            return False

        # Perform soft delete
        space.deleted_at = utc_now()
        self.db.commit()

        # Audit log
        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "kb.space.deleted",
                "resource_type": "kb_space",
                "resource_id": kb_id,
                "status": "success",
            }
        )

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
            self.audit_repo.create(
                {
                    "user_id": user_id,
                    "action": "kb.document.delete.failed",
                    "resource_type": "kb_document",
                    "resource_id": document_id,
                    "status": "failed",
                    "details": {"reason": "not_found_or_unauthorized"},
                }
            )
            return False

        # Perform soft delete
        document.deleted_at = utc_now()

        # Decrement document count and total size on the KB space
        space.document_count = max((space.document_count or 0) - 1, 0)
        space.total_size_bytes = max((space.total_size_bytes or 0) - (document.size_bytes or 0), 0)

        self.db.commit()

        # Audit log
        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "kb.document.deleted",
                "resource_type": "kb_document",
                "resource_id": document_id,
                "details": {"kb_id": document.kb_id, "filename": document.filename},
                "status": "success",
            }
        )

        self._enqueue_wiki_retract(space, document_id)
        return True

    def _enqueue_wiki_retract(self, space: Any, document_id: str) -> None:
        """文档被删后，把它从 Wiki 血缘里摘掉。

        best-effort：Wiki 是原文的衍生物，摘除失败不该让删除本身回滚——残留的引用
        会在下一次收尾时被当作死链清掉。
        """
        try:
            from features.knowledge.wiki.config import wiki_enabled
            from features.knowledge.wiki.jobs import enqueue_retract

            if space is None or not wiki_enabled(space):
                return
            enqueue_retract(self.db, space.kb_id, [document_id])
        except Exception as exc:  # noqa: BLE001
            logger.warning("文档 %s 的 Wiki 血缘摘除入队失败: %s", document_id, exc)

    def create_space(
        self,
        user_id: str,
        name: str,
        description: Optional[str] = None,
        chunk_method: str = "semantic",
        metadata: Optional[Dict[str, Any]] = None,
        visibility: str = "private",
        initial_grants: Optional[list[tuple[str, str]]] = None,
        granted_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new KB space.

        ``visibility`` defaults to ``private`` so existing user-facing callers keep
        their behaviour; admin-managed public KBs pass ``visibility="public"``.

        ``initial_grants`` contains edition-owned principal grants applied atomically after
        creation.  CE supplies an empty list; EE decides which organization principals inherit
        visibility without leaking those contracts into this shared service.
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

        grants = [
            (str(kind).strip(), str(principal_id).strip())
            for kind, principal_id in (initial_grants or [])
            if str(kind).strip() and str(principal_id).strip()
        ]
        if grants:
            from features.knowledge.edition import grant_initial_access

            grant_initial_access(
                self.db, space.kb_id, grants, granted_by=granted_by or user_id,
            )

        # Audit log
        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "kb.space.created",
                "resource_type": "kb_space",
                "resource_id": space.kb_id,
                "status": "success",
            }
        )

        return {
            "kb_id": space.kb_id,
            "name": space.name,
            "description": space.description,
            "document_count": space.document_count,
            "created_at": space.created_at.isoformat(),
        }

    def update_space(
        self,
        kb_id: str,
        user_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        index_modes: Optional[List[str]] = None,
        wiki_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update an existing KB space.

        ``index_modes`` / ``wiki_config`` 落在 metadata 里。改索引模式**不会**自动
        补建索引：新勾选 wiki 后要调重新生成接口，才会把已有文档排进生成队列——
        这一步会实打实消耗模型额度，不该在改配置时悄悄发生。
        """
        space = self.repo.get_space(kb_id)
        if space and self._is_system_managed_space(space):
            self.audit_repo.create(
                {
                    "user_id": user_id,
                    "action": "kb.space.update.failed",
                    "resource_type": "kb_space",
                    "resource_id": kb_id,
                    "status": "failed",
                    "details": {"reason": "system_managed"},
                }
            )
            return None
        if not self._has_kb_level(space, user_id, "admin"):
            self.audit_repo.create(
                {
                    "user_id": user_id,
                    "action": "kb.space.update.failed",
                    "resource_type": "kb_space",
                    "resource_id": kb_id,
                    "status": "failed",
                    "details": {"reason": "not_found_or_unauthorized"},
                }
            )
            return None

        update_data: Dict[str, Any] = {}
        if name is not None:
            update_data["name"] = name
        if description is not None:
            update_data["description"] = description

        if index_modes is not None or wiki_config is not None:
            from features.knowledge.wiki.config import normalize_index_modes

            metadata = dict(space.extra_data or {}) if space else {}
            if index_modes is not None:
                metadata["index_modes"] = normalize_index_modes(index_modes)
            if wiki_config is not None:
                merged = dict(metadata.get("wiki_config") or {})
                merged.update({k: v for k, v in wiki_config.items() if v is not None})
                metadata["wiki_config"] = merged
            # 列名是 metadata，但 ORM 属性叫 extra_data——仓储层是 setattr，必须用属性名
            update_data["extra_data"] = metadata

        updated = self.repo.update_space(kb_id, update_data)
        if not updated:
            return None

        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "kb.space.updated",
                "resource_type": "kb_space",
                "resource_id": kb_id,
                "status": "success",
            }
        )

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
        checksum: Optional[str] = None,
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
        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "kb.document.uploaded",
                "resource_type": "kb_document",
                "resource_id": document.document_id,
                "details": {"kb_id": kb_id, "filename": filename},
                "status": "success",
            }
        )

        return {
            "document_id": document.document_id,
            "kb_id": document.kb_id,
            "title": document.title,
            "filename": document.filename,
            "size_bytes": document.size_bytes,
            "uploaded_at": document.uploaded_at.isoformat(),
        }

    def add_artifact_to_space(self, artifact_id: str, user_id: str, kb_id: str) -> Dict[str, Any]:
        """Create a KB document from an existing user artifact."""
        artifact_repo = ArtifactRepository(self.db)
        artifact = artifact_repo.get_by_id(artifact_id)
        if not artifact:
            raise ValueError("资源不存在或无权限")
        if not can_access_artifact(self.db, user_id, artifact):
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

        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "kb.document.added_from_my_space",
                "resource_type": "kb_document",
                "resource_id": document.document_id,
                "details": {"kb_id": kb_id, "artifact_id": artifact_id},
                "status": "success",
            }
        )

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
