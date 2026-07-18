"""Tests for marketplace community publishing (user submission + admin review + post-publish browse/install).

Covers: submission snapshot and slug derivation (stripping the user fingerprint suffix, collision avoidance), duplicate submission 409, withdrawal rules,
entering the marketplace listing/detail/installable state after approval, going offline on rejection, and category override during review.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import AdminSkill, MarketplaceSubmission
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services import marketplace_service as mk


@pytest.fixture(autouse=True)
def _no_cache_refresh(monkeypatch):
    monkeypatch.setattr(mk, "refresh_skill_caches", lambda: None)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _make_user_skill(db, skill_id: str, owner: str) -> AdminSkill:
    row = AdminSkill(
        skill_id=skill_id,
        skill_content=(
            f"---\nname: {skill_id}\ndescription: 测试用私有技能\n---\n\n# 正文\n\n做点事。\n"
        ),
        display_name="我的测试技能",
        description="测试用私有技能",
        version="1.0.0",
        tags=["测试"],
        extra_files={"scripts/run.py": "print('hi')\n"},
        is_enabled=True,
        owner_user_id=owner,
    )
    db.add(row)
    db.commit()
    return row


def test_submit_snapshot_and_slug_strips_fingerprint(db):
    suffix = mk._user_suffix("userA")
    skill_id = f"my-skill-{suffix}"
    _make_user_skill(db, skill_id, "userA")

    sub = mk.submit_to_marketplace(
        db, skill_id, owner_user_id="userA", submitter_name="张三", note="求上架", category="办公效率"
    )
    assert sub["slug"] == "my-skill"  # fingerprint suffix stripped
    assert sub["status"] == "pending"
    row = db.query(MarketplaceSubmission).filter_by(submission_id=sub["submission_id"]).one()
    assert "测试用私有技能" in row.skill_content
    assert row.extra_files == {"scripts/run.py": "print('hi')\n"}
    assert row.submitter_name == "张三"


def test_slug_collision_with_builtin_dir(db):
    # The preset directory already has diagram-builder, so the community slug auto-avoids the collision
    _make_user_skill(db, "diagram-builder", "userA")
    sub = mk.submit_to_marketplace(db, "diagram-builder", owner_user_id="userA", category="办公效率")
    assert sub["slug"] == "diagram-builder-2"


def test_submit_category_must_be_in_fixed_set(db):
    _make_user_skill(db, "cat-skill", "userA")
    with pytest.raises(BadRequestError):
        mk.submit_to_marketplace(db, "cat-skill", owner_user_id="userA", category="")
    with pytest.raises(BadRequestError):
        mk.submit_to_marketplace(db, "cat-skill", owner_user_id="userA", category="自定义分类")
    sub = mk.submit_to_marketplace(db, "cat-skill", owner_user_id="userA", category="研发效率")
    assert sub["category"] == "研发效率"
    # Changing the category during review is likewise constrained to the fixed set
    with pytest.raises(BadRequestError):
        mk.review_submission(db, sub["submission_id"], approve=True, category="乱填的")


def test_categories_fixed_order(db):
    cats = mk.list_categories(db)
    assert cats[:8] == mk.MARKETPLACE_CATEGORIES


def test_submit_requires_ownership(db):
    _make_user_skill(db, "owned-skill", "userA")
    with pytest.raises(ResourceNotFoundError):
        mk.submit_to_marketplace(db, "owned-skill", owner_user_id="userB", category="办公效率")


def test_duplicate_submission_409(db):
    _make_user_skill(db, "dup-skill", "userA")
    mk.submit_to_marketplace(db, "dup-skill", owner_user_id="userA", category="办公效率")
    with pytest.raises(HTTPException) as e:
        mk.submit_to_marketplace(db, "dup-skill", owner_user_id="userA", category="办公效率")
    assert e.value.status_code == 409


def test_withdraw_pending_ok_but_approved_blocked(db):
    _make_user_skill(db, "wd-skill", "userA")
    sub = mk.submit_to_marketplace(db, "wd-skill", owner_user_id="userA", category="办公效率")
    mk.withdraw_submission(db, sub["submission_id"], owner_user_id="userA")
    assert mk.list_my_submissions(db, "userA") == []

    sub2 = mk.submit_to_marketplace(db, "wd-skill", owner_user_id="userA", category="办公效率")
    mk.review_submission(db, sub2["submission_id"], approve=True)
    with pytest.raises(BadRequestError):
        mk.withdraw_submission(db, sub2["submission_id"], owner_user_id="userA")


def test_approve_publishes_to_listing_detail_and_install(db):
    _make_user_skill(db, "pub-skill", "userA")
    sub = mk.submit_to_marketplace(db, "pub-skill", owner_user_id="userA", submitter_name="张三", category="办公效率")

    # Not in the marketplace while pending
    assert all(it["slug"] != "pub-skill" for it in mk.list_marketplace_skills(db))

    mk.review_submission(db, sub["submission_id"], approve=True, category="数据分析")
    items = mk.list_marketplace_skills(db)
    pub = next(it for it in items if it["slug"] == "pub-skill")
    assert pub["source"] == "community"
    assert pub["author"] == "张三"
    assert pub["category"] == "数据分析"

    detail = mk.get_marketplace_skill("pub-skill", db)
    assert "做点事" in detail["instructions"]
    assert detail["files"] == [{"path": "scripts/run.py", "size": len("print('hi')\n")}]

    # Another user installs: sourced from the snapshot, carrying their own fingerprint
    r = mk.install_marketplace_skill(db, "pub-skill", owner_user_id="userB")
    assert r["action"] == "installed"
    row = db.query(AdminSkill).filter_by(skill_id=r["id"]).one()
    assert row.owner_user_id == "userB"
    assert row.extra_files == {"scripts/run.py": "print('hi')\n"}
    assert mk.is_installed(db, "pub-skill", owner_user_id="userB") is True


def test_reject_approved_takes_offline(db):
    _make_user_skill(db, "off-skill", "userA")
    sub = mk.submit_to_marketplace(db, "off-skill", owner_user_id="userA", category="办公效率")
    mk.review_submission(db, sub["submission_id"], approve=True)
    assert any(it["slug"] == "off-skill" for it in mk.list_marketplace_skills(db))

    mk.review_submission(db, sub["submission_id"], approve=False, review_note="质量不达标")
    assert all(it["slug"] != "off-skill" for it in mk.list_marketplace_skills(db))
    with pytest.raises(ResourceNotFoundError):
        mk.get_marketplace_skill("off-skill", db)
    sub_after = mk.get_submission(db, sub["submission_id"])
    assert sub_after["status"] == "rejected"
    assert sub_after["review_note"] == "质量不达标"


def test_admin_list_orders_pending_first(db):
    _make_user_skill(db, "s1", "userA")
    _make_user_skill(db, "s2", "userA")
    a = mk.submit_to_marketplace(db, "s1", owner_user_id="userA", category="办公效率")
    b = mk.submit_to_marketplace(db, "s2", owner_user_id="userA", category="数据分析")
    mk.review_submission(db, a["submission_id"], approve=True)
    rows = mk.list_submissions(db)
    assert [r["status"] for r in rows] == ["pending", "approved"]
    assert len(mk.list_submissions(db, "pending")) == 1
