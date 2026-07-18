"""Data access layer — audit log repositories.

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


class AuditLogRepository:
    """Repository for audit log operations."""

    def __init__(self, db: Session):
        self.db = db

    def create(self, log_data: Dict[str, Any]) -> AuditLog:
        """Create a new audit log entry."""
        log = AuditLog(**log_data)
        self.db.add(log)
        try:
            self.db.commit()
            self.db.refresh(log)
        except Exception:
            # Audit should not block the main business flow in local/dev setups.
            self.db.rollback()
            logging.getLogger(__name__).debug(
                "audit log write failed for action=%s", log_data.get("action"), exc_info=True
            )
        return log

    def log_denial(
        self,
        *,
        user_id: Optional[str],
        action: str,
        reason: str,
        required: Optional[str] = None,
        actual: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        request: Any = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        """Record a permission-denied event, storing required vs actual and the request context."""
        details: Dict[str, Any] = {"reason": reason}
        if required is not None:
            details["required"] = required
        if actual is not None:
            details["actual"] = actual
        if extra:
            details.update(extra)

        ip_address: Optional[str] = None
        user_agent: Optional[str] = None
        if request is not None:
            try:
                client = getattr(request, "client", None)
                if client is not None:
                    ip_address = client.host
                headers = getattr(request, "headers", None)
                if headers is not None:
                    user_agent = headers.get("user-agent")
            except Exception:
                pass
        # Middleware already pushed trace_id into the ContextVar; request.state has no such field.
        from core.infra.logging import trace_id_var
        trace_id: Optional[str] = trace_id_var.get() or None

        return self.create({
            "user_id": user_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "details": details,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "trace_id": trace_id,
            "status": "failure",
            "error_code": 403,
        })

    def list_by_user(
        self,
        user_id: str,
        action: Optional[str] = None,
        page: int = 1,
        page_size: int = 50
    ) -> tuple[List[AuditLog], int]:
        """List audit logs for a user."""
        query = self.db.query(AuditLog).filter(
            AuditLog.user_id == user_id
        )

        if action:
            query = query.filter(AuditLog.action == action)

        total = query.count()
        logs = query.order_by(desc(AuditLog.created_at)).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        return logs, total

    def get_by_id(self, log_id: int) -> Optional[AuditLog]:
        """Get audit log by ID."""
        return self.db.query(AuditLog).filter(AuditLog.log_id == log_id).first()

    def list_with_filters(
        self,
        user_id: str,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 50
    ) -> tuple[List[AuditLog], int]:
        """List audit logs with multiple filters."""
        query = self.db.query(AuditLog).filter(AuditLog.user_id == user_id)

        if action:
            query = query.filter(AuditLog.action == action)
        if resource_type:
            query = query.filter(AuditLog.resource_type == resource_type)
        if start_date:
            query = query.filter(AuditLog.created_at >= start_date)
        if end_date:
            query = query.filter(AuditLog.created_at <= end_date)

        total = query.count()
        logs = query.order_by(desc(AuditLog.created_at)).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        return logs, total

    def list_all(
        self,
        user_id: Optional[str] = None,
        user_ids: Optional[List[str]] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        status: Optional[str] = None,
        sandbox_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[List[AuditLog], int]:
        """Global audit-log query (for the security admin console, not restricted to a user_id).

        Difference from ``list_with_filters``: user_id is an optional filter rather than a mandatory
        constraint, and it supports filtering by status / sandbox_id, used to investigate login
        failures, permission denials, and tracing by sandbox instance.

        ``user_ids`` is used for "filter by username/name keyword" — the route layer first resolves
        the keyword into a batch of user_ids and passes them in; an empty list means the keyword
        matched nothing and should return an empty result (not the whole table).
        """
        query = self.db.query(AuditLog)
        if user_ids is not None:
            if not user_ids:
                return [], 0
            query = query.filter(AuditLog.user_id.in_(user_ids))
        if user_id:
            query = query.filter(AuditLog.user_id == user_id)
        if action:
            query = query.filter(AuditLog.action == action)
        if resource_type:
            query = query.filter(AuditLog.resource_type == resource_type)
        if status:
            query = query.filter(AuditLog.status == status)
        if sandbox_id:
            query = query.filter(AuditLog.sandbox_id == sandbox_id)
        if start_date:
            query = query.filter(AuditLog.created_at >= start_date)
        if end_date:
            query = query.filter(AuditLog.created_at <= end_date)

        total = query.count()
        logs = (
            query.order_by(desc(AuditLog.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return logs, total

    def distinct_filter_values(self) -> Dict[str, List[str]]:
        """For frontend dropdowns: deduplicated action / resource_type / sandbox_id lists."""
        actions = [
            r[0]
            for r in self.db.query(AuditLog.action).distinct().all()
            if r[0]
        ]
        resource_types = [
            r[0]
            for r in self.db.query(AuditLog.resource_type).distinct().all()
            if r[0]
        ]
        sandbox_ids = [
            r[0]
            for r in self.db.query(AuditLog.sandbox_id)
            .filter(AuditLog.sandbox_id.isnot(None))
            .distinct()
            .all()
            if r[0]
        ]
        return {
            "actions": sorted(actions),
            "resource_types": sorted(resource_types),
            "sandbox_ids": sorted(sandbox_ids),
        }

    def get_global_stats(self, days: int = 7) -> Dict[str, Any]:
        """Global audit overview (last `days` days): total / failures / permission denials / login failures / top actions."""
        from datetime import timedelta

        start_date = datetime.utcnow() - timedelta(days=days)
        base = self.db.query(AuditLog).filter(AuditLog.created_at >= start_date)

        total = base.count()
        # status is constrained to success/failure/error; failure = not success
        failed = base.filter(AuditLog.status != "success").count()
        # Permission denial: denial writes error_code=403
        denied = base.filter(AuditLog.error_code == 403).count()
        # Login failure: action shaped like auth.*.failed / auth.login.failed
        login_failed = base.filter(
            AuditLog.action.like("auth.%"),
            AuditLog.status != "success",
        ).count()

        action_groups = (
            self.db.query(AuditLog.action, func.count(AuditLog.log_id))
            .filter(AuditLog.created_at >= start_date)
            .group_by(AuditLog.action)
            .order_by(desc(func.count(AuditLog.log_id)))
            .limit(10)
            .all()
        )
        top_actions = [{"action": a, "count": c} for a, c in action_groups]

        return {
            "period_days": days,
            "total_actions": total,
            "failed_actions": failed,
            "denied_actions": denied,
            "login_failed": login_failed,
            "top_actions": top_actions,
        }

    def get_user_stats(self, user_id: str, days: int = 7) -> Dict[str, Any]:
        """Get audit statistics for a user."""
        from datetime import timedelta

        start_date = datetime.utcnow() - timedelta(days=days)

        query = self.db.query(AuditLog).filter(
            AuditLog.user_id == user_id,
            AuditLog.created_at >= start_date
        )

        # Total actions
        total = query.count()

        # Failed actions
        failed = query.filter(AuditLog.status == "failed").count()

        # Actions by type
        actions_by_type = {}
        action_groups = self.db.query(
            AuditLog.action, func.count(AuditLog.log_id)
        ).filter(
            AuditLog.user_id == user_id,
            AuditLog.created_at >= start_date
        ).group_by(AuditLog.action).all()

        for action, count in action_groups:
            actions_by_type[action] = count

        # Most active day
        daily_counts = self.db.query(
            func.date(AuditLog.created_at).label('date'),
            func.count(AuditLog.log_id).label('count')
        ).filter(
            AuditLog.user_id == user_id,
            AuditLog.created_at >= start_date
        ).group_by(func.date(AuditLog.created_at)).order_by(desc('count')).first()

        most_active_day = None
        if daily_counts:
            most_active_day = {
                'date': daily_counts.date.isoformat() if hasattr(daily_counts.date, 'isoformat') else str(daily_counts.date),
                'count': daily_counts.count
            }

        return {
            'period_days': days,
            'total_actions': total,
            'failed_actions': failed,
            'success_rate': round((total - failed) / total * 100, 2) if total > 0 else 0,
            'actions_by_type': actions_by_type,
            'most_active_day': most_active_day
        }
