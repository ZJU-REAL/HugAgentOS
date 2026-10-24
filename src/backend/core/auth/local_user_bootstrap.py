"""Create the default CE administrator on a fresh instance."""
from __future__ import annotations
from core.infra.time import utc_now
from typing import Optional
from sqlalchemy.orm import Session
from core.auth.password import hash_password, verify_password
from core.config.settings import settings
from core.db.models import LocalUser, UserShadow
from core.auth import local_users_constants

def ensure_ce_default_admin(db: Session) -> tuple[Optional[str], bool]:
    """Ensure a fresh CE instance has an ``admin/admin`` local administrator.

    The bootstrap credential is marked for mandatory password change. Existing
    passwords are never reset. If a different local account already exists, the
    explicit onboarding choice is preserved and no extra account is created.
    """
    if settings.edition.edition != "ce":
        return None, False

    shadow = db.query(UserShadow).filter(UserShadow.username == local_users_constants.CE_DEFAULT_ADMIN_USERNAME).first()
    created = False

    if shadow is None:
        if db.query(LocalUser).count() > 0:
            return None, False
        user_id = "user_ce_admin"
        shadow = UserShadow(
            user_id=user_id,
            user_center_id=user_id,
            username=local_users_constants.CE_DEFAULT_ADMIN_USERNAME,
            email=None,
            avatar_url=None,
            extra_data={
                "auth_source": "local",
                **local_users_constants.CE_DEFAULT_ADMIN_CAPABILITIES,
                "must_change_password": True,
                "onboarding_required": True,
            },
            last_sync_at=utc_now(),
        )
        db.add(shadow)
        db.flush()
        local = LocalUser(
            user_id=user_id,
            password_hash=hash_password(local_users_constants.CE_DEFAULT_ADMIN_PASSWORD),
            nickname="Administrator",
            status="active",
            password_updated_at=None,
        )
        db.add(local)
        created = True
    else:
        local = db.query(LocalUser).filter(LocalUser.user_id == shadow.user_id).first()
        if local is None:
            local = LocalUser(
                user_id=shadow.user_id,
                password_hash=hash_password(local_users_constants.CE_DEFAULT_ADMIN_PASSWORD),
                nickname="Administrator",
                status="active",
                password_updated_at=None,
            )
            db.add(local)
            created = True

        meta = dict(shadow.extra_data or {})
        meta.update(local_users_constants.CE_DEFAULT_ADMIN_CAPABILITIES)
        meta.setdefault("auth_source", "local")
        try:
            completed_version = int(meta.get("onboarding_completed_version") or 0)
        except (TypeError, ValueError):
            completed_version = 0
        if completed_version < local_users_constants.CE_ONBOARDING_VERSION:
            meta["onboarding_required"] = True
        else:
            meta.pop("onboarding_required", None)
        if verify_password(local_users_constants.CE_DEFAULT_ADMIN_PASSWORD, local.password_hash):
            meta["must_change_password"] = True
        shadow.extra_data = meta
        shadow.updated_at = utc_now()

    db.commit()
    return shadow.user_id, created
