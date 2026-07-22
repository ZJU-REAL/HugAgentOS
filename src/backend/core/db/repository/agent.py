"""Community repository for personal and administrator sub-agents."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.db.models import UserAgent
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session


class UserAgentRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, agent_id: str) -> Optional[UserAgent]:
        return self.db.query(UserAgent).filter(UserAgent.agent_id == agent_id).first()

    def list_for_user(self, user_id: str) -> List[UserAgent]:
        return (
            self.db.query(UserAgent)
            .filter(
                or_(
                    and_(UserAgent.owner_type == "admin", UserAgent.is_enabled.is_(True)),
                    and_(UserAgent.owner_type == "user", UserAgent.user_id == user_id),
                )
            )
            .order_by(UserAgent.owner_type.desc(), UserAgent.sort_order, UserAgent.created_at)
            .all()
        )

    def list_admin(self) -> List[UserAgent]:
        return (
            self.db.query(UserAgent)
            .filter(UserAgent.owner_type == "admin")
            .order_by(UserAgent.sort_order, UserAgent.created_at)
            .all()
        )

    def count_user_agents(self, user_id: str) -> int:
        return (
            self.db.query(func.count(UserAgent.agent_id))
            .filter(UserAgent.owner_type == "user", UserAgent.user_id == user_id)
            .scalar()
            or 0
        )

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


__all__ = ["UserAgentRepository"]
