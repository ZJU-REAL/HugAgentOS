"""Data access layer — team repositories.

Split out of the former monolithic ``core/db/repository.py``. The package
``__init__`` re-exports every repository class, so ``from core.db.repository
import XxxRepository`` keeps working unchanged.
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, func, select
from core.db.models import (
    UserShadow, ChatSession, ChatMessage, CatalogOverride,
    KBSpace, KBDocument, Artifact, AuditLog, UserAgent,
    LocalUser, Team, TeamMember, TeamFolder, InviteCode,
)


class TeamRepository:
    """Team repository."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, team_id: str) -> Optional[Team]:
        return self.db.query(Team).filter(Team.team_id == team_id).first()

    def get_by_name(self, name: str) -> Optional[Team]:
        return self.db.query(Team).filter(Team.name == name).first()

    def list_all(self) -> List[Team]:
        return self.db.query(Team).order_by(Team.created_at.desc()).all()

    def list_for_user(self, user_id: str) -> List[tuple[Team, str]]:
        """Return [(team, role_in_team), ...]."""
        rows = (
            self.db.query(Team, TeamMember.role)
            .join(TeamMember, Team.team_id == TeamMember.team_id)
            .filter(TeamMember.user_id == user_id)
            .order_by(Team.name)
            .all()
        )
        # Return real tuples (Row is not a tuple subclass); the root fix is
        # annotation_typing=False in docker/cython_build.py, this conversion is defensive.
        # See user.py get_by_username for the rationale.
        return [tuple(r) for r in rows]

    def create(self, data: Dict[str, Any]) -> Team:
        team = Team(**data)
        self.db.add(team)
        self.db.commit()
        self.db.refresh(team)
        return team

    def update(self, team_id: str, data: Dict[str, Any]) -> Optional[Team]:
        team = self.get(team_id)
        if not team:
            return None
        for k, v in data.items():
            setattr(team, k, v)
        team.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(team)
        return team

    def delete(self, team_id: str) -> bool:
        team = self.get(team_id)
        if not team:
            return False
        self.db.delete(team)
        self.db.commit()
        return True

    # ── Member management ───────────────────────────────────────
    def list_members(self, team_id: str) -> List[tuple[TeamMember, UserShadow]]:
        rows = (
            self.db.query(TeamMember, UserShadow)
            .join(UserShadow, TeamMember.user_id == UserShadow.user_id)
            .filter(TeamMember.team_id == team_id)
            .order_by(TeamMember.role, TeamMember.joined_at)
            .all()
        )
        # Same as list_for_user: return real tuples (defensive layer).
        return [tuple(r) for r in rows]

    def get_member(self, team_id: str, user_id: str) -> Optional[TeamMember]:
        return (
            self.db.query(TeamMember)
            .filter(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
            .first()
        )

    def get_member_role(self, team_id: str, user_id: str) -> Optional[str]:
        row = (
            self.db.query(TeamMember.role)
            .filter(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
            .first()
        )
        return row[0] if row else None

    def list_for_users_bulk(self, user_ids: List[str]) -> Dict[str, List[tuple[Team, str]]]:
        """Fetch all team memberships for a batch of users in one query, avoiding list_users N+1."""
        if not user_ids:
            return {}
        rows = (
            self.db.query(TeamMember.user_id, Team, TeamMember.role)
            .join(Team, Team.team_id == TeamMember.team_id)
            .filter(TeamMember.user_id.in_(user_ids))
            .order_by(Team.name)
            .all()
        )
        grouped: Dict[str, List[tuple[Team, str]]] = {uid: [] for uid in user_ids}
        for uid, team, role in rows:
            grouped.setdefault(uid, []).append((team, role))
        return grouped

    def member_counts_bulk(self, team_ids: List[str]) -> Dict[str, int]:
        """Get member counts for a batch of teams in one GROUP BY, avoiding list_teams N+1."""
        if not team_ids:
            return {}
        rows = (
            self.db.query(TeamMember.team_id, func.count(TeamMember.user_id))
            .filter(TeamMember.team_id.in_(team_ids))
            .group_by(TeamMember.team_id)
            .all()
        )
        return {tid: count for tid, count in rows}

    def add_member(self, team_id: str, user_id: str, role: str = "member") -> TeamMember:
        existing = self.get_member(team_id, user_id)
        if existing:
            existing.role = role
            self.db.commit()
            return existing
        tm = TeamMember(team_id=team_id, user_id=user_id, role=role)
        self.db.add(tm)
        self.db.commit()
        return tm

    def remove_member(self, team_id: str, user_id: str) -> bool:
        tm = self.get_member(team_id, user_id)
        if not tm:
            return False
        self.db.delete(tm)
        self.db.commit()
        return True

    def set_member_role(self, team_id: str, user_id: str, role: str) -> bool:
        tm = self.get_member(team_id, user_id)
        if not tm:
            return False
        tm.role = role
        self.db.commit()
        return True


class InviteCodeRepository:
    """Invite code repository."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, code: str) -> Optional[InviteCode]:
        return self.db.query(InviteCode).filter(InviteCode.code == code).first()

    def list_all(
        self,
        include_used: bool = True,
        include_revoked: bool = True,
    ) -> List[InviteCode]:
        q = self.db.query(InviteCode)
        if not include_used:
            q = q.filter(InviteCode.used_by.is_(None))
        if not include_revoked:
            q = q.filter(InviteCode.revoked.is_(False))
        return q.order_by(desc(InviteCode.created_at)).all()

    def create(self, data: Dict[str, Any]) -> InviteCode:
        inv = InviteCode(**data)
        self.db.add(inv)
        self.db.commit()
        self.db.refresh(inv)
        return inv

    def revoke(self, code: str) -> bool:
        inv = self.get(code)
        if not inv:
            return False
        inv.revoked = True
        self.db.commit()
        return True

    def delete(self, code: str) -> bool:
        inv = self.get(code)
        if not inv:
            return False
        self.db.delete(inv)
        self.db.commit()
        return True
