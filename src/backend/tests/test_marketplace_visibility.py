"""Marketplace item visibility scope: public/scoped settings → user/team/role three-principal resolution → list/single-item filtering."""

from __future__ import annotations

import pytest

from core.auth.marketplace_visibility import get_hidden_item_ids, is_item_visible
from core.db.models import (
    MarketplaceVisibilityGrant,
    Role,
    RoleAssignment,
    Team,
    TeamMember,
    UserShadow,
)
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services import marketplace_listing as ml


def _seed(db):
    """u_admin=super admin; u_team ∈ t1; u_role directly assigned r1; u_trole ∈ t2 (t2 has department default role r2); u_none has no affiliation."""
    db.add(UserShadow(user_id="u_admin", username="admin", extra_data={"role": "super_admin"}))
    for uid in ("u_team", "u_role", "u_trole", "u_none"):
        db.add(UserShadow(user_id=uid, username=uid, extra_data={}))
    db.add(Team(team_id="t1", name="团队一", owner_user_id="u_team"))
    db.add(Team(team_id="t2", name="团队二", owner_user_id="u_trole"))
    db.add(TeamMember(team_id="t1", user_id="u_team", role="member"))
    db.add(TeamMember(team_id="t2", user_id="u_trole", role="member"))
    db.add(Role(role_id="r1", name="分析师", permissions={}))
    db.add(Role(role_id="r2", name="研发", permissions={}))
    db.add(RoleAssignment(role_id="r1", principal_type="user", principal_id="u_role"))
    db.add(RoleAssignment(role_id="r2", principal_type="team", principal_id="t2"))
    db.commit()


def test_default_public_everyone_visible(db_session):
    db = db_session
    _seed(db)
    # Missing row = public: with no scoped item there are no hidden items for anyone
    assert get_hidden_item_ids(db, ml.KIND_SKILL, "u_none") == set()
    assert is_item_visible(db, ml.KIND_SKILL, "any-skill", "u_none")
    vis = ml.get_listing_visibility(db, ml.KIND_SKILL, "any-skill")
    assert vis["visibility"] == "public" and vis["grants"] == []


def test_scoped_user_team_role_grants(db_session):
    db = db_session
    _seed(db)
    ml.set_listing_visibility(
        db, ml.KIND_SKILL, "s1",
        visibility="scoped",
        grants=[
            {"principal_type": "user", "principal_id": "u_role"},
            {"principal_type": "team", "principal_id": "t1"},
            {"principal_type": "role", "principal_id": "r2"},
        ],
        updated_by="admin",
    )
    # Personal grant / team member / role acquired via team (t2→r2) → visible; unaffiliated → not visible
    assert is_item_visible(db, ml.KIND_SKILL, "s1", "u_role")
    assert is_item_visible(db, ml.KIND_SKILL, "s1", "u_team")
    assert is_item_visible(db, ml.KIND_SKILL, "s1", "u_trole")
    assert not is_item_visible(db, ml.KIND_SKILL, "s1", "u_none")
    # Super admin always visible; anonymous (empty user_id) not visible
    assert is_item_visible(db, ml.KIND_SKILL, "s1", "u_admin")
    assert not is_item_visible(db, ml.KIND_SKILL, "s1", None)
    # kind isolation: the same item_id in a different marketplace is unaffected
    assert is_item_visible(db, ml.KIND_PLUGIN, "s1", "u_none")


def test_role_direct_assignment(db_session):
    db = db_session
    _seed(db)
    ml.set_listing_visibility(
        db, ml.KIND_AGENT, "a1",
        visibility="scoped",
        grants=[{"principal_type": "role", "principal_id": "r1"}],
    )
    # r1 is directly assigned to the individual u_role
    assert is_item_visible(db, ml.KIND_AGENT, "a1", "u_role")
    assert not is_item_visible(db, ml.KIND_AGENT, "a1", "u_team")


def test_annotate_and_filter_visibility(db_session):
    db = db_session
    _seed(db)
    ml.set_listing_visibility(
        db, ml.KIND_PLUGIN, "p1",
        visibility="scoped",
        grants=[{"principal_type": "user", "principal_id": "u_team"}],
    )
    items = [{"slug": "p1"}, {"slug": "p2"}]

    # User side: an ungranted user cannot see p1
    out = ml.annotate_and_filter(
        db, ml.KIND_PLUGIN, [dict(i) for i in items],
        id_key="slug", include_disabled=False, viewer_user_id="u_none",
    )
    assert [i["slug"] for i in out] == ["p2"]
    assert out[0]["visibility"] == "public"

    # User side: a granted user can see it, annotated as scoped
    out = ml.annotate_and_filter(
        db, ml.KIND_PLUGIN, [dict(i) for i in items],
        id_key="slug", include_disabled=False, viewer_user_id="u_team",
    )
    assert {i["slug"]: i["visibility"] for i in out} == {"p1": "scoped", "p2": "public"}

    # Admin side: no filtering, all visible + visibility annotation
    out = ml.annotate_and_filter(
        db, ml.KIND_PLUGIN, [dict(i) for i in items],
        id_key="slug", include_disabled=True,
    )
    assert {i["slug"]: i["visibility"] for i in out} == {"p1": "scoped", "p2": "public"}


def test_set_back_to_public_clears_grants(db_session):
    db = db_session
    _seed(db)
    ml.set_listing_visibility(
        db, ml.KIND_SKILL, "s2",
        visibility="scoped",
        grants=[{"principal_type": "user", "principal_id": "u_role"}],
    )
    assert not is_item_visible(db, ml.KIND_SKILL, "s2", "u_none")
    ml.set_listing_visibility(db, ml.KIND_SKILL, "s2", visibility="public")
    assert is_item_visible(db, ml.KIND_SKILL, "s2", "u_none")
    assert db.query(MarketplaceVisibilityGrant).filter_by(kind=ml.KIND_SKILL, item_id="s2").count() == 0
    # The enable/disable switch is not broken by visibility-scope settings (missing row defaults to enabled → still enabled after upsert)
    assert ml.get_disabled_ids(db, ml.KIND_SKILL) == set()


def test_set_visibility_validation(db_session):
    db = db_session
    _seed(db)
    with pytest.raises(BadRequestError):
        ml.set_listing_visibility(db, ml.KIND_SKILL, "s3", visibility="secret")
    with pytest.raises(BadRequestError):
        ml.set_listing_visibility(db, ml.KIND_SKILL, "s3", visibility="scoped", grants=[])
    with pytest.raises(BadRequestError):
        ml.set_listing_visibility(
            db, ml.KIND_SKILL, "s3",
            visibility="scoped", grants=[{"principal_type": "dept", "principal_id": "x"}],
        )
    # Duplicate grants are deduplicated
    res = ml.set_listing_visibility(
        db, ml.KIND_SKILL, "s3",
        visibility="scoped",
        grants=[
            {"principal_type": "user", "principal_id": "u_role"},
            {"principal_type": "user", "principal_id": "u_role"},
        ],
    )
    assert len(res["grants"]) == 1


def test_ensure_item_visible_guard(db_session):
    db = db_session
    _seed(db)
    ml.set_listing_visibility(
        db, ml.KIND_AGENT, "a2",
        visibility="scoped",
        grants=[{"principal_type": "team", "principal_id": "t1"}],
    )
    ml.ensure_item_visible(db, ml.KIND_AGENT, "a2", "u_team", resource="marketplace_agent")
    with pytest.raises(ResourceNotFoundError):
        ml.ensure_item_visible(db, ml.KIND_AGENT, "a2", "u_none", resource="marketplace_agent")
