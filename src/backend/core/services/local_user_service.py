"""Local user system business logic: registration, login, password change, disabling."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

CE_DEFAULT_ADMIN_USERNAME = "admin"
CE_DEFAULT_ADMIN_PASSWORD = "admin"
CE_DEFAULT_ADMIN_CAPABILITIES: Dict[str, Any] = {
    "role": "super_admin",
    "ce_default_admin": True,
    "lab_enabled": True,
    "can_use_api_key": True,
    "can_add_skill": True,
    "can_add_mcp": True,
    "can_import_plugin": True,
    "can_add_agent": True,
    "can_create_private_kb": True,
    "can_create_public_kb": False,
    "can_create_channel_bot": True,
    "can_switch_model": True,
    "can_run_autonomous_loop": True,
    "can_system_config": True,
    "can_content_manage": True,
    "allowed_apps": "*",
}

from core.auth.password import hash_password, verify_password

# Seam S1: invite codes / team brief list belong to EE — the CE derived tree physically
# lacks these two modules; when missing, this service degrades automatically: open
# registration (no registration code), teams always empty. EE behavior unchanged.
try:
    from core.auth.invite import claim_code, validate_code
except ModuleNotFoundError:
    claim_code = None
    validate_code = None
try:
    from core.services.team_service import list_user_teams_brief
except ModuleNotFoundError:

    def list_user_teams_brief(db, user_id):
        return []


from core.config.settings import settings
from core.db.models import LocalUser, TeamMember, UserShadow
from core.db.repository import (
    AuditLogRepository,
    InviteCodeRepository,
    LocalUserRepository,
    TeamRepository,
    UserRepository,
)


@dataclass
class RegisterResult:
    ok: bool
    message: str
    user_id: Optional[str] = None
    user_info: Optional[Dict[str, Any]] = None


@dataclass
class LoginResult:
    ok: bool
    message: str
    user_id: Optional[str] = None
    user_info: Optional[Dict[str, Any]] = None


class LocalUserService:
    """Encapsulates local account registration, login, and disabling logic. Transactions are completed within this service."""

    def __init__(self, db: Session):
        self.db = db
        self.user_repo = UserRepository(db)
        self.local_repo = LocalUserRepository(db)
        self.team_repo = TeamRepository(db)
        self.invite_repo = InviteCodeRepository(db)
        self.audit_repo = AuditLogRepository(db)

    # ── Registration ───────────────────────────────────────────
    def register(
        self,
        code: str,
        username: str,
        password: str,
        nickname: Optional[str] = None,
        email: Optional[str] = None,
        real_name: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> RegisterResult:
        username = (username or "").strip()
        nickname = (nickname or "").strip()
        email = (email or "").strip().lower() or None
        password = password or ""
        code = (code or "").strip()

        if not username:
            return RegisterResult(False, "账号不能为空")
        if not USERNAME_PATTERN.match(username):
            return RegisterResult(False, "账号只能包含英文、数字、下划线，长度 2-32 位")
        if not nickname:
            return RegisterResult(False, "用户名不能为空")
        if len(nickname) > 32:
            return RegisterResult(False, "用户名长度不能超过 32 位")
        if not email:
            return RegisterResult(False, "邮箱不能为空")
        if not EMAIL_PATTERN.match(email):
            return RegisterResult(False, "邮箱格式不正确")
        if len(password) < settings.auth.password_min_length:
            return RegisterResult(False, f"密码长度至少 {settings.auth.password_min_length} 位")

        # Account uniqueness
        if self.db.query(UserShadow).filter(UserShadow.username == username).first() is not None:
            return RegisterResult(False, "账号已被占用")
        # Email uniqueness
        if self.db.query(UserShadow).filter(UserShadow.email == email).first() is not None:
            return RegisterResult(False, "邮箱已被使用")

        # License seat cap (M4): internal deployments / unlimited seats always pass (counting and copy in licensing/seats.py)
        from core.licensing.seats import seat_block_reason

        seat_block = seat_block_reason(self.db)
        if seat_block:
            return RegisterResult(False, seat_block)

        # First only validate the registration code (no state change); consume it after the user is successfully created
        if validate_code is not None:
            ok, reason, invite = validate_code(self.db, code)
            if not ok:
                return RegisterResult(False, reason or "注册码无效")
        else:
            invite = None

        user_id = f"user_{uuid.uuid4().hex[:16]}"
        try:
            # Create users_shadow
            shadow = UserShadow(
                user_id=user_id,
                user_center_id=user_id,  # local accounts use user_id as center_id
                username=username,
                email=email,
                avatar_url=None,
                extra_data={"auth_source": "local"},
                last_sync_at=datetime.utcnow(),
            )
            self.db.add(shadow)
            self.db.flush()

            # Create local_users
            self.local_repo.create(
                {
                    "user_id": user_id,
                    "password_hash": hash_password(password),
                    "nickname": nickname,
                    "real_name": (real_name or "").strip() or None,
                    "phone": (phone or "").strip() or None,
                    "status": "active",
                    "invited_by_code": invite.code if invite else None,
                    "password_updated_at": datetime.utcnow(),
                }
            )

            # Pre-bind team
            if invite and invite.preset_team_id:
                team = self.team_repo.get(invite.preset_team_id)
                if team is not None:
                    tm = TeamMember(
                        team_id=invite.preset_team_id,
                        user_id=user_id,
                        role=invite.preset_role or "member",
                    )
                    self.db.add(tm)

            # The shadow is now persisted; atomically consume the registration code (conditional UPDATE ensures concurrency safety)
            if claim_code is not None:
                claimed_ok, claim_reason, _ = claim_code(self.db, code, user_id)
                if not claimed_ok:
                    self.db.rollback()
                    return RegisterResult(False, claim_reason or "注册码已被使用")

            # Audit
            self.audit_repo.create(
                {
                    "user_id": user_id,
                    "action": "user.register_local",
                    "resource_type": "user",
                    "resource_id": user_id,
                    "details": {"invite_code": invite.code if invite else None},
                    "status": "success",
                }
            )

            self.db.commit()
        except Exception as exc:  # noqa: BLE001
            self.db.rollback()
            return RegisterResult(False, f"注册失败：{exc}")

        return RegisterResult(
            ok=True,
            message="注册成功",
            user_id=user_id,
            user_info=self._build_user_info(user_id, username),
        )

    # ── Direct account creation by admin ───────────────────────
    def create_by_admin(
        self,
        username: str,
        password: str,
        *,
        nickname: Optional[str] = None,
        email: Optional[str] = None,
        real_name: Optional[str] = None,
        phone: Optional[str] = None,
        status: str = "active",
        team_id: Optional[str] = None,
        team_role: str = "member",
        actor: Optional[str] = None,
    ) -> RegisterResult:
        """Create a local account directly from the Config console — no registration code, optional direct team binding.

        Differences from :meth:`register`: no registration code required, email optional,
        initial status can be specified, team can be bound explicitly.
        """
        username = (username or "").strip()
        nickname = (nickname or "").strip() or username
        email = (email or "").strip().lower() or None
        password = password or ""
        status = (status or "active").strip()
        team_role = (team_role or "member").strip() or "member"

        if not username:
            return RegisterResult(False, "账号不能为空")
        if not USERNAME_PATTERN.match(username):
            return RegisterResult(False, "账号只能包含英文、数字、下划线，长度 2-32 位")
        if len(nickname) > 32:
            return RegisterResult(False, "用户名长度不能超过 32 位")
        if email and not EMAIL_PATTERN.match(email):
            return RegisterResult(False, "邮箱格式不正确")
        if len(password) < settings.auth.password_min_length:
            return RegisterResult(False, f"密码长度至少 {settings.auth.password_min_length} 位")
        if status not in ("active", "disabled", "pending"):
            return RegisterResult(False, "状态不合法")

        # Account uniqueness
        if self.db.query(UserShadow).filter(UserShadow.username == username).first() is not None:
            return RegisterResult(False, "账号已被占用")
        # Email uniqueness (only when an email is provided)
        if (
            email
            and self.db.query(UserShadow).filter(UserShadow.email == email).first() is not None
        ):
            return RegisterResult(False, "邮箱已被使用")

        # Target team existence check (only when provided)
        if team_id:
            if self.team_repo.get(team_id) is None:
                return RegisterResult(False, "目标团队不存在")

        # License seat cap: internal deployments / unlimited seats always pass
        from core.licensing.seats import seat_block_reason

        seat_block = seat_block_reason(self.db)
        if seat_block:
            return RegisterResult(False, seat_block)

        user_id = f"user_{uuid.uuid4().hex[:16]}"
        try:
            shadow = UserShadow(
                user_id=user_id,
                user_center_id=user_id,
                username=username,
                email=email,
                avatar_url=None,
                extra_data={"auth_source": "local"},
                last_sync_at=datetime.utcnow(),
            )
            self.db.add(shadow)
            self.db.flush()

            self.local_repo.create(
                {
                    "user_id": user_id,
                    "password_hash": hash_password(password),
                    "nickname": nickname,
                    "real_name": (real_name or "").strip() or None,
                    "phone": (phone or "").strip() or None,
                    "status": status,
                    "invited_by_code": None,
                    "password_updated_at": datetime.utcnow(),
                }
            )

            if team_id:
                self.db.add(TeamMember(team_id=team_id, user_id=user_id, role=team_role))

            self.audit_repo.create(
                {
                    "user_id": user_id,
                    "action": "user.create_admin",
                    "resource_type": "user",
                    "resource_id": user_id,
                    "details": {
                        "created_by": actor or "config_admin",
                        "team_id": team_id,
                        "team_role": team_role if team_id else None,
                    },
                    "status": "success",
                }
            )

            self.db.commit()
        except Exception as exc:  # noqa: BLE001
            self.db.rollback()
            return RegisterResult(False, f"创建失败：{exc}")

        return RegisterResult(
            ok=True,
            message="创建成功",
            user_id=user_id,
            user_info=self._build_user_info(user_id, username),
        )

    # ── Externally pushed account creation (OA / SSO server-side provisioning) ──
    def _unique_username(self, base: str) -> str:
        """Ensure username uniqueness: append a _1 / _2 … suffix on collision."""
        base = (base or "").strip() or "oa_user"
        candidate = base
        suffix = 0
        while (
            self.db.query(UserShadow).filter(UserShadow.username == candidate).first() is not None
        ):
            suffix += 1
            candidate = f"{base}_{suffix}"
        return candidate

    def get_or_create_external_account(
        self,
        *,
        external_id: str,
        username: Optional[str] = None,
        real_name: Optional[str] = None,
        source: str = "oa_sso",
    ) -> tuple[UserShadow, bool]:
        """Idempotently create a local account keyed by ``users_shadow.user_center_id`` (for external SSO/OA server-side push).

        First call creates users_shadow + local_users (random strong password) and returns
        ``(shadow, True)``; if it already exists, returns ``(shadow, False)`` directly,
        without touching the password or profile.

        Key differences from register / create_by_admin: uses the external ``external_id``
        as ``user_center_id`` (idempotency key); the account name defaults to the
        external_id itself (satisfying "account equals user id"); USERNAME_PATTERN is not
        applied (OA employee numbers are often long digit strings). The password is a
        cryptographically-random strong secret — OA users log in via redirect and never
        use it; it exists only to satisfy the account system and be unguessable.
        **The caller commits the transaction.**
        """
        external_id = (external_id or "").strip()
        if not external_id:
            raise ValueError("external_id is required")

        existing = self.user_repo.get_by_user_center_id(external_id)
        if existing is not None:
            return existing, False

        # License seat cap (M4): blocks new creation only; internal deployments / unlimited seats always pass
        from core.licensing import SeatLimitExceeded
        from core.licensing.seats import seat_block_reason

        block = seat_block_reason(self.db)
        if block:
            raise SeatLimitExceeded(block)

        final_username = self._unique_username((username or external_id).strip() or external_id)
        user_id = f"user_{uuid.uuid4().hex[:16]}"

        shadow = UserShadow(
            user_id=user_id,
            user_center_id=external_id,
            username=final_username,
            email=None,
            avatar_url=None,
            extra_data={"auth_source": source, "external_id": external_id},
            last_sync_at=datetime.utcnow(),
        )
        self.db.add(shadow)
        self.db.flush()

        self.local_repo.create(
            {
                "user_id": user_id,
                "password_hash": hash_password(secrets.token_urlsafe(24)),
                "nickname": (real_name or "").strip() or final_username,
                "real_name": (real_name or "").strip() or None,
                "phone": None,
                "status": "active",
                "invited_by_code": None,
                "password_updated_at": datetime.utcnow(),
            }
        )
        # Auditing is written uniformly by the caller (the OA login route's
        # auth.oa.login.success, with the account_created flag) — this method stays pure,
        # flush-only, and the caller commits to keep the transaction atomic.
        return shadow, True

    # ── Login ──────────────────────────────────────────────────
    def authenticate(self, identifier: str, password: str) -> LoginResult:
        """Log in. identifier can be an account name (username) or an email."""
        identifier = (identifier or "").strip()
        if not identifier or not password:
            return LoginResult(False, "账号或密码为空")

        # An identifier with @ takes the email branch; otherwise the account branch (falling back to email if the account branch misses)
        row = None
        if "@" in identifier:
            row = self.local_repo.get_by_email(identifier.lower())
        else:
            row = self.local_repo.get_by_username(identifier)
            if not row:
                row = self.local_repo.get_by_email(identifier.lower())

        if not row:
            return LoginResult(False, "账号或密码错误")
        local, shadow = row

        if local.status == "disabled":
            return LoginResult(False, "账号已被禁用")
        if local.status == "pending":
            return LoginResult(False, "账号待审核")

        if not verify_password(password, local.password_hash):
            return LoginResult(False, "账号或密码错误")

        return LoginResult(
            ok=True,
            message="登录成功",
            user_id=shadow.user_id,
            user_info=self._build_user_info(
                shadow.user_id, shadow.username, shadow=shadow, local=local
            ),
        )

    # ── Password change ────────────────────────────────────────
    def change_password(self, user_id: str, old_password: str, new_password: str) -> LoginResult:
        local = self.local_repo.get(user_id)
        if not local:
            return LoginResult(False, "账号不存在")
        if not verify_password(old_password, local.password_hash):
            return LoginResult(False, "原密码错误")
        if len(new_password or "") < settings.auth.password_min_length:
            return LoginResult(False, f"新密码长度至少 {settings.auth.password_min_length} 位")
        if verify_password(new_password, local.password_hash):
            return LoginResult(False, "新密码不能与原密码相同")

        local.password_hash = hash_password(new_password)
        local.password_updated_at = datetime.utcnow()
        local.updated_at = datetime.utcnow()
        shadow = self.user_repo.get_by_id(user_id)
        if shadow is not None:
            meta = dict(shadow.extra_data or {})
            meta.pop("must_change_password", None)
            shadow.extra_data = meta
            shadow.updated_at = datetime.utcnow()
        self.db.commit()
        return LoginResult(True, "密码修改成功", user_id=user_id)

    def _build_user_info(
        self,
        user_id: str,
        username: str,
        shadow: Optional[UserShadow] = None,
        local: Optional[LocalUser] = None,
    ) -> Dict[str, Any]:
        shadow = shadow or self.user_repo.get_by_id(user_id)
        if local is None:
            local = self.local_repo.get(user_id)
        return {
            "user_center_id": user_id,
            "username": username,
            "email": shadow.email if shadow else None,
            "avatar_url": shadow.avatar_url if shadow else None,
            "nickname": local.nickname if local else None,
            "real_name": local.real_name if local else None,
            "auth_source": "local",
            "teams": list_user_teams_brief(self.db, user_id),
        }

    # ── Profile update ─────────────────────────────────────────
    def update_profile(
        self,
        user_id: str,
        *,
        nickname: Optional[str] = None,
        real_name: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> LoginResult:
        """Update the editable profile fields of a local account. A None field means leave unchanged."""
        local = self.local_repo.get(user_id)
        if not local:
            return LoginResult(False, "账号不存在")

        if nickname is not None:
            nickname = nickname.strip()
            if not nickname:
                return LoginResult(False, "用户名不能为空")
            if len(nickname) > 32:
                return LoginResult(False, "用户名长度不能超过 32 位")
            local.nickname = nickname
        if real_name is not None:
            rn = real_name.strip()
            local.real_name = rn or None
        if phone is not None:
            ph = phone.strip()
            local.phone = ph or None

        local.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(local)

        shadow = self.user_repo.get_by_id(user_id)
        return LoginResult(
            ok=True,
            message="已更新",
            user_id=user_id,
            user_info=self._build_user_info(
                user_id, shadow.username if shadow else "", shadow=shadow, local=local
            ),
        )


def ensure_ce_default_admin(db: Session) -> tuple[Optional[str], bool]:
    """Ensure a fresh CE instance has an ``admin/admin`` local administrator.

    The bootstrap credential is marked for mandatory password change. Existing
    passwords are never reset. If a different local account already exists, the
    explicit onboarding choice is preserved and no extra account is created.
    """
    if settings.edition.edition != "ce":
        return None, False

    shadow = db.query(UserShadow).filter(UserShadow.username == CE_DEFAULT_ADMIN_USERNAME).first()
    created = False

    if shadow is None:
        if db.query(LocalUser).count() > 0:
            return None, False
        user_id = "user_ce_admin"
        shadow = UserShadow(
            user_id=user_id,
            user_center_id=user_id,
            username=CE_DEFAULT_ADMIN_USERNAME,
            email=None,
            avatar_url=None,
            extra_data={
                "auth_source": "local",
                **CE_DEFAULT_ADMIN_CAPABILITIES,
                "must_change_password": True,
            },
            last_sync_at=datetime.utcnow(),
        )
        db.add(shadow)
        db.flush()
        local = LocalUser(
            user_id=user_id,
            password_hash=hash_password(CE_DEFAULT_ADMIN_PASSWORD),
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
                password_hash=hash_password(CE_DEFAULT_ADMIN_PASSWORD),
                nickname="Administrator",
                status="active",
                password_updated_at=None,
            )
            db.add(local)
            created = True

        meta = dict(shadow.extra_data or {})
        meta.update(CE_DEFAULT_ADMIN_CAPABILITIES)
        meta.setdefault("auth_source", "local")
        if verify_password(CE_DEFAULT_ADMIN_PASSWORD, local.password_hash):
            meta["must_change_password"] = True
        shadow.extra_data = meta
        shadow.updated_at = datetime.utcnow()

    db.commit()
    return shadow.user_id, created
