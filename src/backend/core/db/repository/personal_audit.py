"""No-storage audit repository for the community edition."""

from typing import Any, Dict, List, Optional


class AuditLogRepository:
    """Preserve call signatures while keeping governance storage out of CE."""

    def __init__(self, db):
        self.db = db

    def create(self, log_data: Dict[str, Any]):
        return None

    def log_denial(self, **kwargs):
        return None

    def list_by_user(self, *args, **kwargs) -> tuple[List[Any], int]:
        return [], 0

    def get_by_id(self, log_id: int):
        return None

    def list_with_filters(self, *args, **kwargs) -> tuple[List[Any], int]:
        return [], 0

    def list_all(self, *args, **kwargs) -> tuple[List[Any], int]:
        return [], 0

    def distinct_filter_values(self) -> Dict[str, List[str]]:
        return {"actions": [], "resource_types": [], "sandbox_ids": []}

    def get_global_stats(self, days: int = 7) -> Dict[str, Any]:
        return {
            "period_days": days,
            "total_actions": 0,
            "failed_actions": 0,
            "denied_actions": 0,
            "login_failed": 0,
            "top_actions": [],
        }

    def get_user_stats(self, user_id: str, days: int = 7) -> Dict[str, Any]:
        return {"period_days": days, "total_actions": 0, "failed_actions": 0}
