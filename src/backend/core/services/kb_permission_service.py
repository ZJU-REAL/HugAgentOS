"""Knowledge base permission assignment business logic (implicit authorization model).

The KB management console does not configure visibility: shared bases are visible to
everyone by default, but **once any grant is assigned in "User Management / Team
Management" they switch to being visible only to grantees** (personal grants take
precedence over team grants). Visible-set / retrieval resolution lives in
``core.auth.kb_permissions``; this service only handles the grant CRUD for the two
management pages.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.db.repository import KBGrantRepository, KBRepository


class KBPermissionService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = KBGrantRepository(db)
        self.kb_repo = KBRepository(db)

    # ── Grantable resource list (selection source for the user/team management pages) ──
    def list_grantable_resources(self) -> List[Dict[str, Any]]:
        """All shared knowledge bases: local shared bases + Dify datasets (when enabled). Visibility is not distinguished or displayed."""
        out: List[Dict[str, Any]] = []
        for s in self.kb_repo.list_shared_spaces():
            out.append({
                "resource_id": s.kb_id,
                "resource_type": "local",
                "name": s.name,
                "description": s.description or "",
            })

        try:
            from core.kb.dify_kb import is_dify_enabled, list_datasets
            if is_dify_enabled():
                for ds in list_datasets(page=1, limit=100, timeout=5):
                    ds_id = str(ds.get("id", "")).strip()
                    if not ds_id:
                        continue
                    out.append({
                        "resource_id": ds_id,
                        "resource_type": "dify",
                        "name": ds.get("name", ds_id),
                        "description": ds.get("description") or ds.get("desc") or "",
                    })
        except Exception:
            pass
        return out

    # ── principal perspective (user/team management pages) ─────────────────────
    def get_principal_grants(self, principal_type: str, principal_id: str) -> List[Dict[str, str]]:
        return [
            {"resource_id": g.resource_id, "resource_type": g.resource_type, "level": g.level}
            for g in self.repo.list_for_principal(principal_type, principal_id)
        ]

    def replace_principal_grants(
        self,
        principal_type: str,
        principal_id: str,
        grants: List[Dict[str, str]],
        granted_by: Optional[str] = None,
    ) -> int:
        """Fully replace a user's/team's KB grants ("Save" semantics of the management page). Returns the number of rows written.

        Row-level validation (valid resource_type / level, non-empty id) is handled uniformly by ``replace_for_principal``.
        """
        return self.repo.replace_for_principal(principal_type, principal_id, grants or [], granted_by)
