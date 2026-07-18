"""OA single sign-on service-layer tests: HMAC signature verification + auto account creation (random strong password) + dept_id team binding."""

import dataclasses
import hashlib
import hmac
import time

import pytest

from core.config import settings as settings_mod
from core.db.models import LocalUser, Team, TeamMember, UserShadow
from core.services.oa_sso_service import OASsoError, OASsoService, verify_signature


@pytest.fixture
def oa_cfg():
    """Temporarily replace settings.oa_sso (a frozen dataclass, bypassed via replace + __setattr__)."""
    original = settings_mod.settings.oa_sso

    def apply(**kw):
        new = dataclasses.replace(original, **kw)
        object.__setattr__(settings_mod.settings, "oa_sso", new)
        return new

    yield apply
    object.__setattr__(settings_mod.settings, "oa_sso", original)


# ── HMAC signature verification ─────────────────────────────────────────────

def _sign(secret: str, user_id: str, dept_id: str, ts: str, nonce: str) -> str:
    base = "\n".join([user_id, dept_id, ts, nonce])
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def test_signature_skipped_when_no_secret(oa_cfg):
    oa_cfg(sign_secret="")
    # No exception means it is allowed through (intranet debugging only)
    verify_signature(user_id="u1", dept_id="d1", timestamp="", nonce="", signature="")


def test_signature_valid_and_invalid(oa_cfg):
    secret = "topsecret"
    oa_cfg(sign_secret=secret, sign_ttl_seconds=300)

    ts = str(int(time.time()))
    good = _sign(secret, "2031613182211670018", "D100", ts, "n1")
    verify_signature(user_id="2031613182211670018", dept_id="D100", timestamp=ts, nonce="n1", signature=good)

    with pytest.raises(OASsoError):
        verify_signature(user_id="2031613182211670018", dept_id="D100", timestamp=ts, nonce="n1", signature="deadbeef")


def test_signature_expired(oa_cfg):
    secret = "topsecret"
    oa_cfg(sign_secret=secret, sign_ttl_seconds=60)

    old_ts = str(int(time.time()) - 600)
    sig = _sign(secret, "u1", "", old_ts, "")
    with pytest.raises(OASsoError):
        verify_signature(user_id="u1", dept_id="", timestamp=old_ts, nonce="", signature=sig)


# ── Auto account creation + team binding ────────────────────────────────────

def test_provision_creates_local_account_with_strong_password(db_session, oa_cfg):
    oa_cfg(default_role="member")
    oa_uid = "2031613182211670018"

    service = OASsoService(db_session)
    user, team_id, created = service.provision(oa_user_id=oa_uid, dept_id="D100")
    db_session.commit()

    assert created is True
    # Account name = OA user_id; user_center_id also uses OA user_id as the idempotency key
    assert user.username == oa_uid
    assert user.user_center_id == oa_uid
    assert (user.extra_data or {}).get("auth_source") == "oa_sso"

    # A real local account was created, and the password is a non-empty strong hash (never the plaintext user_id)
    local = db_session.query(LocalUser).filter(LocalUser.user_id == user.user_id).first()
    assert local is not None
    assert local.status == "active"
    assert local.password_hash and oa_uid not in local.password_hash

    # Auto-create a team by dept_id + default member
    assert team_id is not None
    team = db_session.query(Team).filter(Team.team_id == team_id).first()
    assert team.sso_department == "D100"
    assert team.source == "sso_auto"
    member = (
        db_session.query(TeamMember)
        .filter(TeamMember.team_id == team_id, TeamMember.user_id == user.user_id)
        .first()
    )
    assert member.role == "member"


def test_provision_applies_team_default_roles(db_session, oa_cfg):
    """When OA direct-push auto-creates a team, the is_team_default roles should be attached to that team (the fix)."""
    from core.db.models import Role, RoleAssignment

    oa_cfg(default_role="member")
    # One default role + one non-default role, confirm only the former is attached
    db_session.add(Role(role_id="role_def", name="默认部门角色", permissions={}, is_team_default=True))
    db_session.add(Role(role_id="role_plain", name="普通角色", permissions={}, is_team_default=False))
    db_session.commit()

    service = OASsoService(db_session)
    user, team_id, created = service.provision(oa_user_id="oa_roleuser", dept_id="D200")
    db_session.commit()

    assert created is True and team_id is not None
    team_role_ids = {
        rid
        for (rid,) in db_session.query(RoleAssignment.role_id)
        .filter(
            RoleAssignment.principal_type == "team",
            RoleAssignment.principal_id == team_id,
        )
        .all()
    }
    assert team_role_ids == {"role_def"}


def test_provision_is_idempotent(db_session, oa_cfg):
    oa_cfg(default_role="member")
    oa_uid = "oa_999"

    service = OASsoService(db_session)
    user1, team1, created1 = service.provision(oa_user_id=oa_uid, dept_id="D100")
    db_session.commit()
    user2, team2, created2 = service.provision(oa_user_id=oa_uid, dept_id="D100")
    db_session.commit()

    assert created1 is True and created2 is False
    assert user1.user_id == user2.user_id  # No duplicate account creation
    assert team1 == team2

    # No duplicate account creation / no duplicate team join
    assert db_session.query(UserShadow).filter(UserShadow.user_center_id == oa_uid).count() == 1
    assert (
        db_session.query(TeamMember)
        .filter(TeamMember.team_id == team1, TeamMember.user_id == user1.user_id)
        .count()
        == 1
    )


def test_provision_without_dept_skips_team(db_session, oa_cfg):
    oa_cfg(default_role="member")
    service = OASsoService(db_session)
    user, team_id, created = service.provision(oa_user_id="oa_nodept", dept_id=None)
    db_session.commit()

    assert created is True
    assert team_id is None
    assert db_session.query(TeamMember).filter(TeamMember.user_id == user.user_id).count() == 0


def test_provision_username_collision_gets_suffixed(db_session, oa_cfg):
    oa_cfg(default_role="member")
    # Pre-occupy username = "dup" (different user_center_id)
    db_session.add(UserShadow(user_id="user_pre", user_center_id="pre_center", username="dup"))
    db_session.commit()

    service = OASsoService(db_session)
    user, _, created = service.provision(oa_user_id="dup", dept_id=None)
    db_session.commit()

    assert created is True
    assert user.user_center_id == "dup"
    assert user.username != "dup"  # Suffixed after a collision
    assert user.username.startswith("dup")


def test_provision_missing_user_id_rejected(db_session, oa_cfg):
    service = OASsoService(db_session)
    with pytest.raises(OASsoError) as ei:
        service.provision(oa_user_id="  ", dept_id="D100")
    assert ei.value.status_code == 400


# ── One-time ticket (redirect login token exchange) ─────────────────────────

async def test_ticket_is_single_use(monkeypatch):
    from core.auth import oa_ticket_store
    from core.auth.oa_ticket_store import consume_ticket, issue_ticket

    monkeypatch.setattr(oa_ticket_store, "_use_memory_store", lambda: True)

    ticket = await issue_ticket({"user_id": "user_1", "dept_id": "D100"})
    first = await consume_ticket(ticket)
    assert first is not None
    assert first["user_id"] == "user_1"
    assert first["dept_id"] == "D100"

    # Single use: the second attempt must come up empty (replay protection)
    second = await consume_ticket(ticket)
    assert second is None


async def test_ticket_invalid_returns_none(monkeypatch):
    from core.auth import oa_ticket_store
    from core.auth.oa_ticket_store import consume_ticket

    monkeypatch.setattr(oa_ticket_store, "_use_memory_store", lambda: True)

    assert await consume_ticket("") is None
    assert await consume_ticket("not-a-real-ticket") is None
