"""Tests for role capability packs + four-layer resolution (personal explicit → role union → team default → system default)."""

from __future__ import annotations

from core.auth.capabilities import resolve_capabilities, resolve_user_capabilities
from core.auth.role_permissions import (
    merge_role_permissions,
    normalize_role_permissions,
    role_permissions_for_user,
)
from core.db.models import Role, RoleAssignment, Team, TeamMember, UserShadow
from core.services.role_service import RoleService


# ── Pure functions: role normalization / merge ────────────────────────────────────
def test_normalize_role_only_keeps_granted_true():
    n = normalize_role_permissions(
        {"can_add_skill": True, "can_add_mcp": False, "junk": "x", "allowed_apps": ["a", "a", "b"]}
    )
    assert n == {"can_add_skill": True, "allowed_apps": ["a", "b"]}
    assert normalize_role_permissions(None) == {}


def test_merge_role_union():
    m = merge_role_permissions([
        {"can_add_skill": True, "allowed_apps": ["a"]},
        {"can_use_api_key": True, "allowed_apps": ["b"]},
    ])
    assert m["can_add_skill"] is True and m["can_use_api_key"] is True
    assert m["allowed_apps"] == ["a", "b"]


# ── Pure functions: four-layer fall-through ────────────────────────────────────
def test_resolve_role_beats_team():
    # Role grants True, team default False → role wins (the role layer sits above the team layer)
    r = resolve_capabilities({}, {"can_use_api_key": True}, {"can_use_api_key": False})
    assert r["can_use_api_key"] is True


def test_resolve_personal_off_beats_role_on():
    r = resolve_capabilities({"can_add_skill": False}, {"can_add_skill": True}, {})
    assert r["can_add_skill"] is False


def test_resolve_team_fills_when_role_silent():
    # Role is silent, team default on → team wins
    r = resolve_capabilities({}, {}, {"can_add_skill": True})
    assert r["can_add_skill"] is True


def test_resolve_allowed_apps_role_layer_priority():
    r = resolve_capabilities({}, {"allowed_apps": ["a", "b"]}, {"allowed_apps": ["c"]})
    assert r["allowed_apps"] == ["a", "b"]


def test_personal_all_apps_overrides_team_restriction():
    from core.auth.capabilities import ALL_APPS
    # Team restricts to 3 apps, personal "force all" sentinel → final is all (None), overriding the team restriction
    r = resolve_capabilities({"allowed_apps": ALL_APPS}, {}, {"allowed_apps": ["a", "b"]})
    assert r["allowed_apps"] is None
    # Personal does not set allowed_apps → follows the team restriction
    r2 = resolve_capabilities({}, {}, {"allowed_apps": ["a", "b"]})
    assert r2["allowed_apps"] == ["a", "b"]


def test_backward_compat_single_layer():
    # Old signature resolve_capabilities(meta, team_defaults) semantics unchanged
    r = resolve_capabilities({}, {"can_add_skill": True})
    assert r["can_add_skill"] is True and r["can_use_api_key"] is False


# ── DB integration ────────────────────────────────────────────────────
def _mk_user(db, uid: str, meta: dict | None = None) -> None:
    db.add(UserShadow(user_id=uid, username=uid, extra_data=meta or {}))


def _mk_role(db, rid: str, perms: dict) -> None:
    db.add(Role(role_id=rid, name=rid, permissions=perms))


def test_ce_degrades_to_empty_without_rows(db_session):
    # No role assignments at all → role_permissions_for_user returns {}
    _mk_user(db_session, "u_norole")
    db_session.commit()
    assert role_permissions_for_user(db_session, "u_norole") == {}


def test_direct_user_role_grants_capability(db_session):
    _mk_user(db_session, "ur1")
    _mk_role(db_session, "r_skill", {"can_add_skill": True})
    db_session.add(RoleAssignment(role_id="r_skill", principal_type="user", principal_id="ur1"))
    db_session.commit()
    caps = resolve_user_capabilities(db_session, "ur1")
    assert caps["can_add_skill"] is True
    assert caps["can_use_api_key"] is False  # not granted → system default


def test_team_role_inherited_by_member(db_session):
    # Department default role: assigned to a team → members inherit in real time
    _mk_user(db_session, "ur2")
    _mk_role(db_session, "r_dept", {"can_system_config": True})
    db_session.add(Team(team_id="t_dept", name="t_dept"))
    db_session.add(TeamMember(team_id="t_dept", user_id="ur2", role="member"))
    db_session.add(RoleAssignment(role_id="r_dept", principal_type="team", principal_id="t_dept"))
    db_session.commit()
    caps = resolve_user_capabilities(db_session, "ur2")
    assert caps["can_system_config"] is True


def test_multi_role_union_and_personal_override(db_session):
    _mk_user(db_session, "ur3", {"can_add_skill": False})  # personal force-off
    _mk_role(db_session, "r_a", {"can_add_skill": True, "can_add_mcp": True})
    _mk_role(db_session, "r_b", {"can_use_api_key": True})
    db_session.add(RoleAssignment(role_id="r_a", principal_type="user", principal_id="ur3"))
    db_session.add(RoleAssignment(role_id="r_b", principal_type="user", principal_id="ur3"))
    db_session.commit()
    caps = resolve_user_capabilities(db_session, "ur3")
    assert caps["can_add_skill"] is False  # personal override beats role
    assert caps["can_add_mcp"] is True     # role r_a union
    assert caps["can_use_api_key"] is True  # role r_b union


# ── Service CRUD + assignment ─────────────────────────────────────────
def test_service_create_assign_delete(db_session):
    svc = RoleService(db_session)
    res = svc.create_role("部门管理员", description="dept admin", permissions={"can_add_skill": True, "junk": 1})
    assert res.ok and res.role_id
    rid = res.role_id
    roles = svc.list_roles()
    assert any(r["role_id"] == rid and r["permissions"] == {"can_add_skill": True} for r in roles)

    # Duplicate-name rejection
    assert not svc.create_role("部门管理员").ok

    # Assign to user
    _mk_user(db_session, "us1")
    db_session.commit()
    svc.set_principal_roles("user", "us1", [rid])
    assert [r["role_id"] for r in svc.get_principal_roles("user", "us1")] == [rid]
    assert svc.list_assignments(rid) == [{"principal_type": "user", "principal_id": "us1"}]

    # Delete role → cascade clears assignments
    assert svc.delete_role(rid).ok
    assert role_permissions_for_user(db_session, "us1") == {}


def test_seed_default_roles_creates_then_idempotent(db_session):
    from core.services.role_service import DEFAULT_ROLES, seed_default_roles

    added = seed_default_roles(db_session)
    assert set(added) == {spec["name"] for spec in DEFAULT_ROLES}
    # Run again: dedup by name → no duplicate creation
    assert seed_default_roles(db_session) == []
    names = {r.name for r in RoleService(db_session).repo.list_all()}
    assert {"部门成员", "IT管理员"}.issubset(names)
    # Seed capability pack is already normalized (only granted bits + allowed_apps kept)
    it = RoleService(db_session).repo.get_by_name("IT管理员")
    assert it.permissions.get("can_system_config") is True
    assert it.permissions.get("allowed_apps") == ["plan_mode", "automation", "batch_runner"]


def test_seed_skips_existing_name(db_session):
    from core.services.role_service import seed_default_roles

    # Admin already created a role with the same name (different id, different capabilities) → seed should not overwrite
    db_session.add(Role(role_id="r_custom", name="部门成员", permissions={"can_add_skill": True}))
    db_session.commit()
    added = seed_default_roles(db_session)
    assert "部门成员" not in added
    assert RoleService(db_session).repo.get_by_name("部门成员").role_id == "r_custom"


def test_apply_team_default_roles_idempotent(db_session):
    from core.db.models import Team
    from core.db.repository import RoleRepository
    from core.services.role_service import apply_team_default_roles, seed_default_roles

    seed_default_roles(db_session)  # 部门成员=new-team default, IT管理员=no
    db_session.add(Team(team_id="t_new", name="t_new"))
    db_session.commit()
    assert apply_team_default_roles(db_session, "t_new") == 1
    assert RoleRepository(db_session).list_principal_role_ids("team", "t_new") == ["role_seed_dept_member"]
    assert apply_team_default_roles(db_session, "t_new") == 0  # idempotent


def test_create_team_auto_assigns_default_role(db_session):
    from core.db.repository import RoleRepository
    from core.services.role_service import seed_default_roles
    from core.services.team_service import TeamService

    seed_default_roles(db_session)
    res = TeamService(db_session).create_team(name="新团队X")
    assert res.ok
    ids = RoleRepository(db_session).list_principal_role_ids("team", res.team_id)
    assert "role_seed_dept_member" in ids and "role_seed_it_admin" not in ids


def test_service_delete_system_role_blocked(db_session):
    db_session.add(Role(role_id="r_sys", name="内置", permissions={}, is_system=True))
    db_session.commit()
    res = RoleService(db_session).delete_role("r_sys")
    assert not res.ok and "内置" in res.message
