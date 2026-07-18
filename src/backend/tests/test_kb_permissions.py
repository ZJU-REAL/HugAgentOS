"""Knowledge base permission assignment system tests: resolver (hidden-by-default / allowlist) + grant repository + permission service.

Hidden-by-default model (KB management has no visibility UI):
  - Shared KBs are hidden from everyone by default; only granted users/teams can see them.
  - private KBs belong to the owner only; owner/super-admin are always admin.
  - Grant precedence: personal grants override team grants.
Create permissions split into two: can_create_private_kb / can_create_public_kb.
"""

import pytest

from core.db.models import KBSpace, Team, TeamMember, UserShadow
from core.db.repository import KBGrantRepository
from core.auth import kb_permissions as kp
from core.services.kb_permission_service import KBPermissionService


@pytest.fixture
def seeded(db_session):
    db = db_session
    db.add_all([
        UserShadow(user_id="u_owner", username="owner"),
        UserShadow(user_id="u_alice", username="alice"),
        UserShadow(user_id="u_bob", username="bob"),
        UserShadow(user_id="u_admin", username="admin", extra_data={"role": "super_admin"}),
        UserShadow(user_id="system_public_kb", username="系统公共"),
        Team(team_id="t1", name="T1"),
        TeamMember(team_id="t1", user_id="u_alice", role="member"),
        KBSpace(kb_id="kb_pub", user_id="system_public_kb", name="Pub", visibility="public"),
        KBSpace(kb_id="kb_shared", user_id="system_public_kb", name="Shared", visibility="public"),
        KBSpace(kb_id="kb_priv", user_id="u_owner", name="Priv", visibility="private"),
    ])
    db.commit()
    r = KBGrantRepository(db)
    r.upsert("kb_shared", "local", "team", "t1", "edit", "u_admin")   # team t1 → edit
    r.upsert("kb_shared", "local", "user", "u_bob", "view", "u_admin")  # bob → view
    return db


# ── Hidden-by-default / allowlist ───────────────────────────────────────────────

def test_unshared_kb_hidden_from_everyone(seeded):
    # kb_pub has no grants → ordinary users cannot see it
    assert "kb_pub" not in kp.get_accessible_local_kb_levels(seeded, "u_alice")
    assert "kb_pub" not in kp.get_accessible_local_kb_levels(seeded, "u_bob")
    assert kp.resolve_local_kb_level(seeded, "u_alice", "kb_pub") == "none"


def test_only_granted_can_see(seeded):
    assert kp.get_accessible_local_kb_levels(seeded, "u_alice")["kb_shared"] == "edit"  # inherited from team
    assert kp.get_accessible_local_kb_levels(seeded, "u_bob")["kb_shared"] == "view"    # direct grant
    assert "kb_shared" not in kp.get_accessible_local_kb_levels(seeded, "u_owner")      # not granted, not visible


def test_grant_then_revoke(seeded):
    KBGrantRepository(seeded).upsert("kb_pub", "local", "user", "u_alice", "view", "u_admin")
    assert kp.get_accessible_local_kb_levels(seeded, "u_alice")["kb_pub"] == "view"
    # Revoke (replace all with empty) → invisible again
    KBGrantRepository(seeded).replace_for_principal("user", "u_alice", [])
    assert "kb_pub" not in kp.get_accessible_local_kb_levels(seeded, "u_alice")


def test_personal_overrides_team(seeded):
    KBGrantRepository(seeded).upsert("kb_shared", "local", "user", "u_alice", "view", "u_admin")
    assert kp.get_accessible_local_kb_levels(seeded, "u_alice")["kb_shared"] == "view"
    assert kp.resolve_local_kb_level(seeded, "u_alice", "kb_shared") == "view"


def test_private_owner_only(seeded):
    assert kp.get_accessible_local_kb_levels(seeded, "u_owner")["kb_priv"] == "admin"
    assert "kb_priv" not in kp.get_accessible_local_kb_levels(seeded, "u_alice")


def test_super_admin_sees_all(seeded):
    lv = kp.get_accessible_local_kb_levels(seeded, "u_admin")
    assert lv == {"kb_pub": "admin", "kb_shared": "admin", "kb_priv": "admin"}


def test_filter_blocks_unauthorized(seeded):
    assert kp.filter_accessible_kb_ids(seeded, "u_bob", ["kb_priv", "kb_shared", "kb_pub"]) == ["kb_shared"]
    assert kp.filter_accessible_kb_ids(seeded, "u_owner", ["kb_shared"]) == []


def test_has_kb_permission_ordering():
    assert kp.has_kb_permission("admin", "edit")
    assert not kp.has_kb_permission("view", "edit")
    assert not kp.has_kb_permission("none", "view")


# ── Service ─────────────────────────────────────────────────────────────────────

def test_service_list_grantable(seeded):
    by_id = {r["resource_id"]: r for r in KBPermissionService(seeded).list_grantable_resources()}
    assert "kb_pub" in by_id and "kb_shared" in by_id
    assert "kb_priv" not in by_id
    assert "visibility" not in by_id["kb_pub"]


def test_service_replace_and_get_principal_grants(seeded):
    svc = KBPermissionService(seeded)
    svc.replace_principal_grants("team", "t1", [
        {"resource_id": "kb_pub", "resource_type": "local", "level": "view"},
    ], granted_by="config_admin")
    assert {g["resource_id"]: g["level"] for g in svc.get_principal_grants("team", "t1")} == {"kb_pub": "view"}


def test_capability_defaults_split_create_perms():
    from core.auth.capabilities import BOOL_CAPABILITY_DEFAULTS
    assert BOOL_CAPABILITY_DEFAULTS.get("can_create_private_kb") is False
    assert BOOL_CAPABILITY_DEFAULTS.get("can_create_public_kb") is False
    assert "can_create_kb" not in BOOL_CAPABILITY_DEFAULTS
