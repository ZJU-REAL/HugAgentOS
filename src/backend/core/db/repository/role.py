"""Data access layer — roles / role assignments.

Isomorphic to [[KBGrantRepository]]: ``set_principal_roles`` fully replaces a
principal's roles (the "Save" semantics of the user/team management page).
``principal_type`` is generalized (user/team, with department reserved) to leave a seam
for "later refactoring into an org tree".
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.db.models import Role, RoleAssignment, TeamMember


class RoleRepository:
    """Roles + role-assignments Repository."""

    def __init__(self, db: Session):
        self.db = db

    # ── Role CRUD ────────────────────────────────────────────────
    def get(self, role_id: str) -> Optional[Role]:
        return self.db.query(Role).filter(Role.role_id == role_id).first()

    def get_by_name(self, name: str) -> Optional[Role]:
        return self.db.query(Role).filter(Role.name == name).first()

    def list_all(self) -> List[Role]:
        return self.db.query(Role).order_by(Role.created_at.desc()).all()

    def list_team_default_role_ids(self) -> List[str]:
        """IDs of roles marked as "new-team default" — auto-assigned when creating/syncing a team."""
        return [
            rid
            for (rid,) in self.db.query(Role.role_id).filter(Role.is_team_default.is_(True)).all()
        ]

    def create(self, data: Dict[str, Any]) -> Role:
        role = Role(**data)
        self.db.add(role)
        self.db.commit()
        self.db.refresh(role)
        return role

    def update(self, role_id: str, data: Dict[str, Any]) -> Optional[Role]:
        role = self.get(role_id)
        if not role:
            return None
        for k, v in data.items():
            setattr(role, k, v)
        role.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(role)
        return role

    def delete(self, role_id: str) -> bool:
        role = self.get(role_id)
        if not role:
            return False
        # Explicitly clear assignments (FK ondelete=CASCADE is not enforced by default in SQLite; deleting manually keeps both sides consistent)
        self.db.query(RoleAssignment).filter(RoleAssignment.role_id == role_id).delete()
        self.db.delete(role)
        self.db.commit()
        return True

    # ── Assignments ──────────────────────────────────────────────
    def list_assignments(self, role_id: str) -> List[RoleAssignment]:
        return self.db.query(RoleAssignment).filter(RoleAssignment.role_id == role_id).all()

    def assignment_counts_bulk(self, role_ids: List[str]) -> Dict[str, int]:
        """Fetch assignment counts for a batch of roles at once (avoids list_roles N+1)."""
        if not role_ids:
            return {}
        from sqlalchemy import func

        rows = (
            self.db.query(RoleAssignment.role_id, func.count())
            .filter(RoleAssignment.role_id.in_(role_ids))
            .group_by(RoleAssignment.role_id)
            .all()
        )
        return {rid: cnt for rid, cnt in rows}

    def list_principal_role_ids(self, principal_type: str, principal_id: str) -> List[str]:
        """Role IDs directly assigned to a principal (excluding inheritance)."""
        return [
            rid
            for (rid,) in self.db.query(RoleAssignment.role_id)
            .filter(
                RoleAssignment.principal_type == principal_type,
                RoleAssignment.principal_id == principal_id,
            )
            .all()
        ]

    def list_roles_for_principal(self, principal_type: str, principal_id: str) -> List[Role]:
        """Role objects directly assigned to a principal (excluding inheritance)."""
        return (
            self.db.query(Role)
            .join(RoleAssignment, Role.role_id == RoleAssignment.role_id)
            .filter(
                RoleAssignment.principal_type == principal_type,
                RoleAssignment.principal_id == principal_id,
            )
            .order_by(Role.name)
            .all()
        )

    def direct_role_ids_for_users_bulk(self, user_ids: List[str]) -> Dict[str, List[str]]:
        """Role IDs **directly** assigned to each user in a batch (principal=user, excluding team inheritance)."""
        if not user_ids:
            return {}
        rows = (
            self.db.query(RoleAssignment.principal_id, RoleAssignment.role_id)
            .filter(
                RoleAssignment.principal_type == "user",
                RoleAssignment.principal_id.in_(user_ids),
            )
            .all()
        )
        grouped: Dict[str, List[str]] = {uid: [] for uid in user_ids}
        for uid, rid in rows:
            grouped.setdefault(uid, []).append(rid)
        return grouped

    def team_role_ids_bulk(self, team_ids: List[str]) -> Dict[str, List[str]]:
        """Role IDs assigned to each team in a batch (department default roles). Used for batch resolution in list_users, to avoid N+1."""
        if not team_ids:
            return {}
        rows = (
            self.db.query(RoleAssignment.principal_id, RoleAssignment.role_id)
            .filter(
                RoleAssignment.principal_type == "team",
                RoleAssignment.principal_id.in_(team_ids),
            )
            .all()
        )
        grouped: Dict[str, List[str]] = {}
        for tid, rid in rows:
            grouped.setdefault(tid, []).append(rid)
        return grouped

    def set_principal_roles(
        self, principal_type: str, principal_id: str, role_ids: List[str]
    ) -> int:
        """Fully replace a principal's role assignments (the "Save" semantics of the
        user/team management page). Returns the number of rows written.

        Only accepts role_ids that actually exist; writes after deduplication.
        """
        self.db.query(RoleAssignment).filter(
            RoleAssignment.principal_type == principal_type,
            RoleAssignment.principal_id == principal_id,
        ).delete()
        valid = {
            rid
            for (rid,) in self.db.query(Role.role_id).filter(Role.role_id.in_(role_ids or [])).all()
        }
        count = 0
        for rid in dict.fromkeys(role_ids or []):  # dedupe while preserving order
            if rid not in valid:
                continue
            self.db.add(
                RoleAssignment(
                    role_id=rid, principal_type=principal_type, principal_id=principal_id
                )
            )
            count += 1
        self.db.commit()
        return count

    def add_principal_roles(
        self, principal_type: str, principal_id: str, role_ids: List[str]
    ) -> int:
        """Incrementally append role assignments to a principal (existing ones are skipped, none are removed). Returns the number of rows added."""
        if not role_ids:
            return 0
        existing = set(self.list_principal_role_ids(principal_type, principal_id))
        valid = {
            rid
            for (rid,) in self.db.query(Role.role_id).filter(Role.role_id.in_(role_ids)).all()
        }
        count = 0
        for rid in dict.fromkeys(role_ids):
            if rid in existing or rid not in valid:
                continue
            self.db.add(
                RoleAssignment(
                    role_id=rid, principal_type=principal_type, principal_id=principal_id
                )
            )
            count += 1
        if count:
            self.db.commit()
        return count

    def purge_principal(self, principal_type: str, principal_id: str) -> int:
        """Delete all of a principal's role assignments (cleanup when deleting a user/team). Returns the number of rows deleted."""
        n = (
            self.db.query(RoleAssignment)
            .filter(
                RoleAssignment.principal_type == principal_type,
                RoleAssignment.principal_id == principal_id,
            )
            .delete()
        )
        self.db.commit()
        return n
