"""Tests for core.services.marketplace_service (skill marketplace install path).

Covers: list/detail reads, id namespacing of a user's private install, independent copies per user,
same-user reinstall upsert, admin global install, credential injection (secrets.json + SKILL.md
credentials section), the installed flag, cross-owner conflict 409. ``refresh_skill_caches`` goes
through the global loader/engine, unrelated to this test's in-memory DB, so it is uniformly
monkeypatched into a no-op.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config.catalog_loader import resolve_skill_detail
from core.db.engine import Base
from core.db.models import AdminSkill
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


def test_list_and_detail_nonempty():
    items = mk.list_marketplace_skills()
    assert len(items) >= 5
    slugs = {it["slug"] for it in items}
    assert "diagram-builder" in slugs
    # Built-in resident skills are prepended; featured entries lead the
    # installable marketplace portion that follows them.
    installable_items = [item for item in items if not item.get("builtin")]
    assert installable_items[0]["featured"] is True
    detail = mk.get_marketplace_skill("diagram-builder")
    assert detail["instructions"]
    assert any(f["path"] == "SKILL.md" for f in detail["files"]) is False  # SKILL.md is the body, not in files
    assert detail["files"]


def test_guizang_ppt_bundle_is_compact_installable_and_dependency_free(db):
    detail = mk.get_marketplace_skill("guizang-ppt-skill")
    paths = {item["path"] for item in detail["files"]}

    assert detail["source"] == "github"
    assert detail["version"] == "2026.8.7"
    assert "不是可编辑的 PPTX" in detail["summary"]
    assert "LICENSE" in paths
    assert "UPSTREAM.md" in paths
    assert "assets/template.html" in paths
    assert "assets/template-swiss.html" in paths
    assert "assets/motion.min.js" in paths
    assert "assets/ppt-skill-showcase.png" not in paths
    assert not any(path.startswith(".github/") for path in paths)
    assert len(paths) <= 30
    assert sum(item["size"] for item in detail["files"]) < 3 * 1024 * 1024
    assert "<SKILL_ROOT>" not in detail["instructions"]
    assert "git -C" not in detail["instructions"]
    assert 'cp "{baseDir}/assets/motion.min.js"' in detail["instructions"]

    result = mk.install_marketplace_skill(
        db, "guizang-ppt-skill", owner_user_id=None, secrets={}
    )
    assert result["dependencies"] == {
        "pip": [], "npm": [], "apt": [], "warnings": []
    }
    row = db.query(AdminSkill).filter_by(skill_id="guizang-ppt-skill").one()
    assert row.dependencies == result["dependencies"]
    assert "assets/motion.min.js" in row.extra_files
    assert "assets/screenshot-backgrounds/style-a/dune.webp" in row.extra_files


def test_unknown_slug_404():
    from core.infra.exceptions import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        mk.get_marketplace_skill("does-not-exist")


def test_user_install_is_namespaced_and_private(db):
    r = mk.install_marketplace_skill(db, "diagram-builder", owner_user_id="userA", secrets={})
    assert r["owner"] == "self" and r["action"] == "installed"
    row = db.query(AdminSkill).filter_by(skill_id=r["id"]).one()
    assert row.owner_user_id == "userA"
    assert row.is_enabled is True
    assert row.user_intro is None
    assert (
        resolve_skill_detail(row.user_intro, row.skill_content)
        == mk.get_marketplace_skill("diagram-builder")["instructions"]
    )
    # frontmatter name is rewritten to the install id
    assert f"name: {r['id']}" in row.skill_content.split("---")[1]
    assert r["id"] != "diagram-builder"  # private install carries a fingerprint suffix


def test_two_users_get_independent_copies(db):
    a = mk.install_marketplace_skill(db, "diagram-builder", owner_user_id="userA", secrets={})
    b = mk.install_marketplace_skill(db, "diagram-builder", owner_user_id="userB", secrets={})
    assert a["id"] != b["id"]
    assert db.query(AdminSkill).filter(AdminSkill.skill_id.in_([a["id"], b["id"]])).count() == 2


def test_same_user_reinstall_upserts(db):
    a = mk.install_marketplace_skill(db, "diagram-builder", owner_user_id="userA", secrets={})
    a2 = mk.install_marketplace_skill(db, "diagram-builder", owner_user_id="userA", secrets={})
    assert a2["id"] == a["id"] and a2["action"] == "updated"
    assert db.query(AdminSkill).filter_by(skill_id=a["id"]).count() == 1


def test_admin_install_is_global(db):
    r = mk.install_marketplace_skill(db, "diagram-builder", owner_user_id=None, secrets={})
    assert r["owner"] == "global" and r["id"] == "diagram-builder"
    row = db.query(AdminSkill).filter_by(skill_id="diagram-builder").one()
    assert row.owner_user_id is None


def test_secrets_injection(db):
    r = mk.install_marketplace_skill(
        db, "gpt-image2-pro", owner_user_id="userA",
        secrets={"IMAGE_GEN_API_KEY": "sk-test-123"},
    )
    row = db.query(AdminSkill).filter_by(skill_id=r["id"]).one()
    assert "secrets.json" in (row.extra_files or {})
    assert json.loads(row.extra_files["secrets.json"])["IMAGE_GEN_API_KEY"] == "sk-test-123"
    assert "凭据配置" in row.skill_content


def test_missing_required_secret_rejected(db, monkeypatch):
    # Temporarily mark gpt-image2-pro's optional credential as required, verifying that missing it gives 400.
    real = mk._read_manifest

    def fake(slug):
        m = real(slug)
        if m and slug == "gpt-image2-pro":
            m = dict(m)
            m["required_secrets"] = [{"key": "IMAGE_GEN_API_KEY", "label": "Key", "required": True}]
        return m

    monkeypatch.setattr(mk, "_read_manifest", fake)
    from core.infra.exceptions import BadRequestError

    with pytest.raises(BadRequestError):
        mk.install_marketplace_skill(db, "gpt-image2-pro", owner_user_id="userA", secrets={})


def test_annotate_installed_flags(db):
    mk.install_marketplace_skill(db, "diagram-builder", owner_user_id="userA", secrets={})
    items = mk.annotate_installed(mk.list_marketplace_skills(), db, owner_user_id="userA")
    by_slug = {it["slug"]: it for it in items}
    assert by_slug["diagram-builder"]["installed"] is True
    # another, uninstalled skill is False
    other = next(
        slug for slug, item in by_slug.items()
        if slug != "diagram-builder" and not item.get("builtin")
    )
    assert by_slug[other]["installed"] is False
    # From userB's view all installable entries are uninstalled; resident
    # built-ins remain available to every user.
    items_b = mk.annotate_installed(mk.list_marketplace_skills(), db, owner_user_id="userB")
    assert all(it["installed"] is False for it in items_b if not it.get("builtin"))
    assert all(it["installed"] is True for it in items_b if it.get("builtin"))


def test_cross_owner_conflict_409(db):
    # Seed a global skill that occupies the namespace id userC's private install would use.
    install_id = "diagram-builder-" + mk._user_suffix("userC")
    db.add(AdminSkill(
        skill_id=install_id, skill_content="x", display_name="x", description="x",
        version="1", tags=[], allowed_tools=[], extra_files={}, dependencies={},
        is_enabled=True, owner_user_id=None,
    ))
    db.commit()
    with pytest.raises(HTTPException) as ei:
        mk.install_marketplace_skill(db, "diagram-builder", owner_user_id="userC", secrets={})
    assert ei.value.status_code == 409


def _zip_skill(name: str, *, description: str = "A test skill.", extra: dict | None = None) -> bytes:
    """Build a minimal skill zip in memory (wrapped in an extra top-level directory, to test prefix stripping)."""
    import io
    import zipfile

    md = f"---\nname: {name}\ndescription: {description}\nversion: 1.2.3\n---\n\n# {name}\n\nbody\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/SKILL.md", md)
        zf.writestr(f"{name}/reference.md", "ref content")
        for path, content in (extra or {}).items():
            zf.writestr(f"{name}/{path}", content)
    return buf.getvalue()


def test_admin_upload_publishes_to_marketplace_not_global(db):
    """Admin uploads a zip -> listed on the marketplace as approved (no global AdminSkill created), installable."""
    data = _zip_skill("my-uploaded-skill")
    res = mk.publish_skill_zip_to_marketplace(db, data, category="办公效率")
    assert res["action"] == "published" and res["skill_id"] == "my-uploaded-skill"

    # no global skill created (not in catalog)
    assert db.query(AdminSkill).filter(AdminSkill.skill_id == "my-uploaded-skill").first() is None

    # appears on the marketplace (community source)
    items = mk.list_marketplace_skills(db)
    m = next((it for it in items if it["slug"] == res["slug"]), None)
    assert m is not None and m["source"] == "community" and m["category"] == "办公效率"

    # can be installed as a global skill
    inst = mk.install_marketplace_skill(db, res["slug"], owner_user_id=None, secrets={})
    assert inst["action"] == "installed"
    glob = db.query(AdminSkill).filter(AdminSkill.skill_id == "my-uploaded-skill", AdminSkill.owner_user_id.is_(None)).first()
    assert glob is not None
    assert glob.user_intro is None
    assert "# my-uploaded-skill" in resolve_skill_detail(glob.user_intro, glob.skill_content)


def test_admin_upload_reupload_updates_same_record(db):
    """Re-uploading the same skill -> updates the existing listing record (same slug), no new one."""
    first = mk.publish_skill_zip_to_marketplace(db, _zip_skill("dup-skill", description="v1"), category="文档处理")
    second = mk.publish_skill_zip_to_marketplace(db, _zip_skill("dup-skill", description="v2"), category="数据分析")
    assert second["action"] == "updated" and second["slug"] == first["slug"]
    from core.db.models import MarketplaceSubmission
    rows = db.query(MarketplaceSubmission).filter(MarketplaceSubmission.skill_id == "dup-skill").all()
    assert len(rows) == 1 and rows[0].category == "数据分析"


def test_admin_upload_rejects_bad_category(db):
    from core.infra.exceptions import BadRequestError
    with pytest.raises(BadRequestError):
        mk.publish_skill_zip_to_marketplace(db, _zip_skill("cat-skill"), category="不存在的分类")


def test_admin_create_form_publishes_to_marketplace(db):
    """Admin "create skill" form -> listed on the marketplace (no global AdminSkill created), installable."""
    content = (
        "---\nname: form-skill\ndescription: built from form.\nversion: 2.0.0\n---\n\n# form-skill\n步骤\n"
    )
    res = mk.publish_skill_to_marketplace(
        db, skill_id="form-skill", skill_content=content, display_name="表单技能",
        description="built from form.", user_intro="## 用户介绍\n\n适合表单自动化。",
        version="2.0.0", tags=["a", "b"], category="研发效率",
    )
    assert res["action"] == "published" and res["slug"] == "form-skill"
    # no global skill created
    assert db.query(AdminSkill).filter(AdminSkill.skill_id == "form-skill").first() is None
    # appears on the marketplace, installable
    m = next((it for it in mk.list_marketplace_skills(db) if it["slug"] == "form-skill"), None)
    assert m is not None and m["category"] == "研发效率" and m["display_name"] == "表单技能"
    inst = mk.install_marketplace_skill(db, "form-skill", owner_user_id=None, secrets={})
    assert inst["action"] == "installed"
    installed = db.query(AdminSkill).filter(
        AdminSkill.skill_id == "form-skill", AdminSkill.owner_user_id.is_(None)
    ).first()
    assert installed is not None
    assert installed.user_intro == "## 用户介绍\n\n适合表单自动化。"


def test_market_summary_is_not_used_as_user_intro():
    assert mk._build_user_intro({"summary": "市场卡片的一句话摘要"}) == ""
    assert (
        mk._build_user_intro({"summary": "摘要", "user_intro": "  ## 详细介绍  "})
        == "## 详细介绍"
    )


def test_whitespace_user_intro_falls_back_to_skill_body():
    raw = "---\nname: fallback-skill\ndescription: 一句话\n---\n\n# 完整正文\n\n操作步骤。\n"
    assert resolve_skill_detail("  \n\t", raw) == "# 完整正文\n\n操作步骤。"


def test_admin_create_form_rejects_bad_category(db):
    from core.infra.exceptions import BadRequestError
    with pytest.raises(BadRequestError):
        mk.publish_skill_to_marketplace(
            db, skill_id="x-skill", skill_content="---\nname: x-skill\ndescription: d\n---\n",
            display_name="X", description="d", version="1.0.0", tags=[], category="乱填",
        )
