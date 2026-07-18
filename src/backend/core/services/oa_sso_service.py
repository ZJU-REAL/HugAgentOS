"""OA single sign-on (server-side direct-push provisioning).

Integration model (a separate path from the ticket verification in core/auth/sso.py):

  The OA backend has already authenticated the user on its side → pushes
  ``user_id`` + ``dept_id`` + signature to this platform
    → platform verifies the signature (HMAC, shared secret between OA and platform)
    → auto-creates a local account (username = user_id, random strong password;
      idempotent: reused if it already exists)
    → binds a team by dept_id (default member role)
    → issues a session token in the response; OA uses it for the login redirect

The trust anchor is on the OA side. The platform does not trust the user_id in the
request body; server-to-server trust is established by HMAC signature verification
with the shared secret agreed between OA and the platform — otherwise "knowing a
valid employee ID = becoming that person". Verification is enforced once
``OA_SSO_SIGN_SECRET`` is configured; leaving it empty is for intranet integration
testing only (a warning is logged).
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from core.config.settings import settings
from core.db.models import Team, TeamMember, UserShadow
from core.infra.logging import get_logger
from core.services.local_user_service import LocalUserService

logger = get_logger(__name__)


class OASsoError(Exception):
    """OA SSO provisioning failure. Carries the HTTP status code and business code for the route layer to render the envelope."""

    def __init__(self, message: str, *, status_code: int = 401, code: int = 30002):
        self.message = message
        self.status_code = status_code
        self.code = code
        super().__init__(message)


def verify_signature(
    *,
    user_id: str,
    dept_id: str,
    timestamp: str,
    nonce: str,
    signature: str,
) -> None:
    """Verify the HMAC-SHA256 signature of an OA server-side request (with timestamp replay protection).

    Signature base string (canonical, newline-joined, fixed order)::

        user_id \n dept_id \n timestamp \n nonce

    HMAC-SHA256(secret, base) → hex; the OA side and platform side use the same
    algorithm. When ``OA_SSO_SIGN_SECRET`` is empty, verification is skipped
    (intranet integration testing only) and a warning reminds that production
    must configure the secret.
    """
    secret = settings.oa_sso.sign_secret
    if not secret:
        logger.warning(
            "oa_sso_signature_skipped",
            reason="OA_SSO_SIGN_SECRET 未配置——生产环境必须配密钥，否则任意调用方可冒充任意工号",
            user_id=user_id,
        )
        return

    if not signature or not timestamp:
        raise OASsoError("Missing signature or timestamp", status_code=401, code=30005)

    # Timestamp replay protection: reject outright when outside the tolerance window
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise OASsoError("Invalid timestamp", status_code=401, code=30005)
    skew = abs(int(time.time()) - ts)
    if skew > settings.oa_sso.sign_ttl_seconds:
        raise OASsoError("Signature expired (timestamp out of window)", status_code=401, code=30005)

    base = "\n".join([user_id, dept_id or "", str(timestamp), nonce or ""])
    expected = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        logger.warning("oa_sso_signature_mismatch", user_id=user_id)
        raise OASsoError("Signature verification failed", status_code=401, code=30005)


class OASsoService:
    """OA direct-push login: auto-create a local account + bind a team by dept_id."""

    def __init__(self, db: Session):
        self.db = db
        self.local_service = LocalUserService(db)

    def _bind_team(self, *, user_id: str, dept_id: Optional[str]) -> Optional[str]:
        """Bind the org team by dept_id and return the team_id. The team is auto-created when it does not exist.

        Uses dept_id as the team identifier (stored in ``Team.sso_department``).
        Idempotent: repeated calls only ensure membership; they neither re-create the
        team nor change existing roles. The member's role within the team is decided
        by ``OA_SSO_DEFAULT_ROLE``; on the **first auto-creation of a team**, all
        default roles with ``Role.is_team_default`` are also attached to that team
        (members inherit their capability bits in real time), aligned with the
        behavior of ``sso_sync.sync_user_department``.
        """
        key = (dept_id or "").strip()
        if not key:
            return None

        role = settings.oa_sso.default_role
        team = self.db.query(Team).filter(Team.sso_department == key).first()
        if team is None:
            team = Team(
                team_id=f"team_{uuid.uuid4().hex[:16]}",
                name=key,
                description=f"由 OA 机构「{key}」自动创建",
                sso_department=key,
                source="sso_auto",
            )
            self.db.add(team)
            self.db.flush()
            logger.info("oa_sso_team_auto_created", team_id=team.team_id, dept_id=key)
            # Auto-attach the "new team default" roles (members inherit in real time)
            # — aligned with sso_sync.sync_user_department; the OA direct-push path
            # previously missed this step, so configured default roles never took
            # effect for OA users. flush-only, keeping the "caller commits"
            # convention; CE without role tables / any exception is skipped safely.
            try:
                from core.db.models import Role, RoleAssignment

                default_ids = [
                    rid
                    for (rid,) in self.db.query(Role.role_id)
                    .filter(Role.is_team_default.is_(True))
                    .all()
                ]
                for rid in default_ids:
                    self.db.add(
                        RoleAssignment(
                            role_id=rid, principal_type="team", principal_id=team.team_id
                        )
                    )
                if default_ids:
                    self.db.flush()
                    logger.info(
                        "oa_sso_team_default_roles_applied",
                        team_id=team.team_id,
                        count=len(default_ids),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("oa_sso_team_default_roles_failed", error=str(exc))

        existing = (
            self.db.query(TeamMember)
            .filter(TeamMember.team_id == team.team_id, TeamMember.user_id == user_id)
            .first()
        )
        if existing is None:
            self.db.add(
                TeamMember(
                    team_id=team.team_id,
                    user_id=user_id,
                    role=role,
                    joined_at=datetime.utcnow(),
                )
            )
            self.db.flush()
            logger.info("oa_sso_user_joined_team", team_id=team.team_id, user_id=user_id, role=role)
        return team.team_id

    def provision(
        self,
        *,
        oa_user_id: str,
        dept_id: Optional[str],
    ) -> Tuple[UserShadow, Optional[str], bool]:
        """End to end: auto-create/fetch the local account → bind the org team.

        Returns ``(user_shadow, team_id, created)``, where ``created`` indicates
        whether an account was newly created this time. The transaction is
        committed by the caller.
        """
        oa_user_id = (oa_user_id or "").strip()
        if not oa_user_id:
            raise OASsoError("Missing user_id", status_code=400, code=30001)

        user, created = self.local_service.get_or_create_external_account(
            external_id=oa_user_id,
            username=oa_user_id,
            source="oa_sso",
        )
        team_id = self._bind_team(user_id=user.user_id, dept_id=dept_id)
        return user, team_id, created
