"""Data access layer — user agent repositories.

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


class UserAgentRepository:
    """Repository for user agent (sub-agent) operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, agent_id: str) -> Optional[UserAgent]:
        return self.db.query(UserAgent).filter(UserAgent.agent_id == agent_id).first()

    def list_for_user(self, user_id: str) -> List[UserAgent]:
        """Return all agents visible to a user: enabled admin agents + user's own agents
        + team agents — every member sees *enabled* team agents; team owner/admin also
        see *disabled* ones (so they can re-enable them; there is no separate admin view)."""
        member_team_ids = self.db.query(TeamMember.team_id).filter(TeamMember.user_id == user_id)
        manager_team_ids = self.db.query(TeamMember.team_id).filter(
            TeamMember.user_id == user_id,
            TeamMember.role.in_(("owner", "admin")),
        )
        return self.db.query(UserAgent).filter(
            or_(
                and_(UserAgent.owner_type == "admin", UserAgent.is_enabled == True),
                and_(UserAgent.owner_type == "user", UserAgent.user_id == user_id),
                and_(
                    UserAgent.owner_type == "team",
                    UserAgent.is_enabled == True,
                    UserAgent.team_id.in_(member_team_ids),
                ),
                and_(UserAgent.owner_type == "team", UserAgent.team_id.in_(manager_team_ids)),
            )
        ).order_by(UserAgent.owner_type.desc(), UserAgent.sort_order, UserAgent.created_at).all()

    def list_admin(self) -> List[UserAgent]:
        """Return all admin-owned agents."""
        return self.db.query(UserAgent).filter(
            UserAgent.owner_type == "admin"
        ).order_by(UserAgent.sort_order, UserAgent.created_at).all()

    def count_user_agents(self, user_id: str) -> int:
        """Count agents owned by a specific user."""
        return self.db.query(func.count(UserAgent.agent_id)).filter(
            UserAgent.owner_type == "user",
            UserAgent.user_id == user_id,
        ).scalar() or 0

    def count_team_agents(self, team_id: str) -> int:
        """Count agents belonging to a specific team."""
        return self.db.query(func.count(UserAgent.agent_id)).filter(
            UserAgent.owner_type == "team",
            UserAgent.team_id == team_id,
        ).scalar() or 0

    def list_for_team(self, team_id: str) -> List[UserAgent]:
        """Return all agents of a team (enabled + disabled), for managers."""
        return self.db.query(UserAgent).filter(
            UserAgent.owner_type == "team",
            UserAgent.team_id == team_id,
        ).order_by(UserAgent.sort_order, UserAgent.created_at).all()

    def create(self, data: Dict[str, Any]) -> UserAgent:
        agent = UserAgent(**data)
        self.db.add(agent)
        self.db.commit()
        self.db.refresh(agent)
        return agent

    def update(self, agent_id: str, data: Dict[str, Any]) -> Optional[UserAgent]:
        agent = self.get_by_id(agent_id)
        if not agent:
            return None
        for key, value in data.items():
            setattr(agent, key, value)
        agent.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(agent)
        return agent

    def delete(self, agent_id: str) -> bool:
        agent = self.get_by_id(agent_id)
        if not agent:
            return False
        self.db.delete(agent)
        self.db.commit()
        return True
