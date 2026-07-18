"""Role permission business logic.

A role = a reusable named capability grant bundle, assigned to a team (= department
default role, inherited by members in real time) or to an individual.
Capability bit normalization reuses ``normalize_role_permissions`` from
[[role_permissions]] (only granted bits are stored).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.auth.role_permissions import normalize_role_permissions
from core.db.models import Role
from core.db.repository import AuditLogRepository, RoleRepository


# Default built-in role seeds: auto-created on startup in a new environment (empty
# roles table), ready to use across platform deployments.
# Idempotent, deduplicated by "role name" — an existing role with the same name is
# skipped (does not overwrite an admin's changes).
DEFAULT_ROLES: List[dict] = [
    {
        "role_id": "role_seed_dept_member",
        "name": "部门成员",
        "description": "部门普通成员的默认能力",
        "is_team_default": True,  # auto-attach this role when creating/syncing a team
        "permissions": {
            "can_add_skill": True,
            "can_add_mcp": True,
            "can_add_agent": True,
            "can_use_api_key": True,
            "can_import_plugin": True,
            "can_create_private_kb": True,
            "can_create_public_kb": True,
            "can_create_channel_bot": True,
            "allowed_apps": ["plan_mode", "automation", "batch_runner"],
        },
    },
    {
        "role_id": "role_seed_it_admin",
        "name": "IT管理员",
        "description": "部门 IT 管理员（含后台访问权限）",
        "permissions": {
            "lab_enabled": True,
            "can_add_skill": True,
            "can_add_mcp": True,
            "can_add_agent": True,
            "can_use_api_key": True,
            "can_import_plugin": True,
            "can_create_private_kb": True,
            "can_create_public_kb": True,
            "can_create_channel_bot": True,
            "can_system_config": True,
            "can_content_manage": True,
            "allowed_apps": ["plan_mode", "automation", "batch_runner"],
        },
    },
]


def seed_default_roles(db: Session) -> List[str]:
    """Seed default roles in a new environment (idempotent, deduplicated by name).
    Returns the list of role names created this time.

    An existing role with the same name → skipped (does not overwrite the admin's
    existing config); in CE, where there is no roles table, the caller's try/except
    fallback degrades this to a no-op.
    """
    repo = RoleRepository(db)
    added: List[str] = []
    for spec in DEFAULT_ROLES:
        if repo.get_by_name(spec["name"]) or repo.get(spec["role_id"]):
            continue
        repo.create(
            {
                "role_id": spec["role_id"],
                "name": spec["name"],
                "description": spec.get("description"),
                "permissions": normalize_role_permissions(spec["permissions"]),
                "is_system": False,
                "is_team_default": bool(spec.get("is_team_default")),
            }
        )
        added.append(spec["name"])
    return added


def apply_team_default_roles(db: Session, team_id: str) -> int:
    """Append all "new-team default" roles to a (newly created/synced) team, inherited
    by members in real time. Returns the number of rows added.

    Idempotent: already-attached ones are skipped. In CE, where there is no roles table,
    the caller's try/except degrades this to a no-op.
    """
    repo = RoleRepository(db)
    return repo.add_principal_roles("team", team_id, repo.list_team_default_role_ids())


def serialize_role(role: Role, *, assignment_count: Optional[int] = None) -> dict:
    """Role → brief structure for the frontend."""
    data: Dict[str, Any] = {
        "role_id": role.role_id,
        "name": role.name,
        "description": role.description,
        "permissions": dict(role.permissions or {}),
        "is_system": bool(role.is_system),
        "is_team_default": bool(role.is_team_default),
        "created_at": role.created_at.isoformat() if role.created_at else None,
        "updated_at": role.updated_at.isoformat() if role.updated_at else None,
    }
    if assignment_count is not None:
        data["assignment_count"] = assignment_count
    return data


@dataclass
class RoleResult:
    ok: bool
    message: str
    role_id: Optional[str] = None


class RoleService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = RoleRepository(db)
        self.audit_repo = AuditLogRepository(db)

    # ── List ─────────────────────────────────────────────────
    def list_roles(self) -> List[dict]:
        roles = self.repo.list_all()
        counts = self.repo.assignment_counts_bulk([r.role_id for r in roles])
        return [serialize_role(r, assignment_count=counts.get(r.role_id, 0)) for r in roles]

    # ── CRUD ─────────────────────────────────────────────────
    def create_role(
        self,
        name: str,
        description: Optional[str] = None,
        permissions: Optional[dict] = None,
        is_team_default: bool = False,
        actor: Optional[str] = None,
    ) -> RoleResult:
        name = (name or "").strip()
        if not name:
            return RoleResult(False, "角色名称不能为空")
        if len(name) > 64:
            return RoleResult(False, "角色名称过长（≤64）")
        if self.repo.get_by_name(name):
            return RoleResult(False, "角色名称已存在")

        role_id = f"role_{uuid.uuid4().hex[:16]}"
        self.repo.create(
            {
                "role_id": role_id,
                "name": name,
                "description": description,
                "permissions": normalize_role_permissions(permissions),
                "is_system": False,
                "is_team_default": bool(is_team_default),
            }
        )
        return RoleResult(True, "角色已创建", role_id)

    def update_role(
        self,
        role_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        permissions: Optional[dict] = None,
        is_team_default: Optional[bool] = None,
        actor: Optional[str] = None,
    ) -> RoleResult:
        role = self.repo.get(role_id)
        if not role:
            return RoleResult(False, "角色不存在")
        data: Dict[str, Any] = {}
        if is_team_default is not None:
            data["is_team_default"] = bool(is_team_default)
        if name is not None:
            name = name.strip()
            if not name:
                return RoleResult(False, "角色名称不能为空")
            if len(name) > 64:
                return RoleResult(False, "角色名称过长（≤64）")
            existing = self.repo.get_by_name(name)
            if existing and existing.role_id != role_id:
                return RoleResult(False, "角色名称已存在")
            data["name"] = name
        if description is not None:
            data["description"] = description
        if permissions is not None:
            data["permissions"] = normalize_role_permissions(permissions)
        if data:
            self.repo.update(role_id, data)
        return RoleResult(True, "角色已更新", role_id)

    def delete_role(self, role_id: str, actor: Optional[str] = None) -> RoleResult:
        role = self.repo.get(role_id)
        if not role:
            return RoleResult(False, "角色不存在")
        if role.is_system:
            return RoleResult(False, "内置角色不可删除")
        self.repo.delete(role_id)  # cascade-clear assignments
        return RoleResult(True, "角色已删除", role_id)

    # ── Assignments ──────────────────────────────────────────
    def list_assignments(self, role_id: str) -> Optional[List[dict]]:
        """Which principals (users/teams) a given role is assigned to. Returns None if
        the role does not exist."""
        if not self.repo.get(role_id):
            return None
        return [
            {"principal_type": a.principal_type, "principal_id": a.principal_id}
            for a in self.repo.list_assignments(role_id)
        ]

    def set_principal_roles(
        self, principal_type: str, principal_id: str, role_ids: List[str]
    ) -> int:
        """Fully replace a principal's (user/team) role assignments; returns the number
        of rows written."""
        return self.repo.set_principal_roles(principal_type, principal_id, role_ids)

    def get_principal_roles(self, principal_type: str, principal_id: str) -> List[dict]:
        """Brief list of roles directly assigned to a principal (excluding inheritance)."""
        return [serialize_role(r) for r in self.repo.list_roles_for_principal(principal_type, principal_id)]
