"""Single source of truth for seat counting (shared by CE/EE).

The definition of "one seat" appears here exactly once: shared by the config
panel display (seats_used) and enforcement before adding a user
(seat_available — local registration, SSO auto account creation), so the two
never drift. Under CE / internal / unlimited-seat license, seat_available is
always True.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from core.db.models import UserShadow

from .manager import license_manager


def seats_used(db: Session) -> int:
    """Current occupied seat count = total number of users_shadow rows (including SSO shadow accounts)."""
    return db.query(UserShadow).count()


def seat_available(db: Session) -> bool:
    """Seat check before adding a user."""
    return license_manager.seats_allow(seats_used(db))


SEAT_LIMIT_MESSAGE = "已达 license 席位上限，请联系管理员扩容或更新 license"
LICENSE_BLOCK_MESSAGE = "当前 license 无效或未激活，无法新增用户——请联系管理员在 系统配置 → License 上传有效 license"


def seat_block_reason(db: Session) -> str | None:
    """Reason text when adding a user is rejected; returns None when allowed.

    seats_allow is always False under expired/invalid/missing modes — in that case
    the real reason is the license state rather than the seat count, so the text
    must be distinguished; otherwise a new deployment (path configured but no file
    provided) would be misled by "seat limit" into investigating a nonexistent
    seat problem.
    """
    if seat_available(db):
        return None
    mode = license_manager.mode()
    return SEAT_LIMIT_MESSAGE if mode in ("licensed", "grace") else LICENSE_BLOCK_MESSAGE
