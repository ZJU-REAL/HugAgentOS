"""Tests for core.services.skill_deps_aggregator."""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import AdminSkill
from core.services.skill_deps_aggregator import (
    aggregate_all,
    render_apt_packages,
    render_npm_package_json,
    render_pip_requirements,
    write_manifests,
)


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


def _mk(skill_id: str, deps: dict, *, enabled: bool = True) -> AdminSkill:
    return AdminSkill(
        skill_id=skill_id,
        skill_content="---\nname: x\n---\nbody",
        display_name=skill_id,
        description="d",
        version="1.0.0",
        tags=[],
        allowed_tools=[],
        extra_files={},
        dependencies=deps,
        is_enabled=enabled,
    )


def test_no_skills(db):
    out = aggregate_all(db)
    assert "pandas" not in out["pip"]
    # Headers still rendered
    assert "DO NOT EDIT" in out["pip"]
    assert json.loads(out["npm"])["dependencies"] == {}
    assert "DO NOT EDIT" in out["apt"]


def test_disabled_skills_ignored(db):
    db.add(_mk("x", {"pip": [{"name": "pandas", "version": ">=1.0", "source": "manual"}]}, enabled=False))
    db.commit()
    out = aggregate_all(db)
    assert "pandas" not in out["pip"]


def test_merge_dedup(db):
    db.add(_mk("a", {"pip": [{"name": "pandas", "version": ">=1.0", "source": "requirements.txt"}]}))
    db.add(_mk("b", {"pip": [{"name": "pandas", "version": ">=1.5,<2.0", "source": "static_scan"}]}))
    db.add(_mk("c", {"pip": [{"name": "numpy", "source": "static_scan"}]}))
    db.commit()
    pip = render_pip_requirements(db)
    # More specific (longer) constraint wins
    assert "pandas>=1.5,<2.0" in pip
    # bare → plain line
    assert "\nnumpy\n" in pip


def test_manual_wins_over_other_sources(db):
    db.add(_mk("a", {"pip": [{"name": "pandas", "version": ">=1.0", "source": "requirements.txt"}]}))
    db.add(_mk("b", {"pip": [{"name": "pandas", "version": "==2.1.0", "source": "manual"}]}))
    db.commit()
    pip = render_pip_requirements(db)
    assert "pandas==2.1.0" in pip
    assert "pandas>=1.0" not in pip


def test_caret_version_falls_back_to_bare(db):
    """Poetry-style `^1.2` is not pip-compatible — strip it."""
    db.add(_mk("a", {"pip": [{"name": "pandas", "version": "^1.0", "source": "pyproject.toml"}]}))
    db.commit()
    pip = render_pip_requirements(db)
    # `pandas` line with no operator
    assert "\npandas\n" in pip
    assert "^" not in pip


def test_bare_numeric_version_becomes_ge(db):
    db.add(_mk("a", {"pip": [{"name": "pandas", "version": "1.5.0", "source": "manual"}]}))
    db.commit()
    pip = render_pip_requirements(db)
    assert "pandas>=1.5.0" in pip


def test_npm_package_json(db):
    db.add(_mk("a", {"npm": [{"name": "pptxgenjs", "version": "^3.0", "source": "package.json"}]}))
    db.add(_mk("b", {"npm": [{"name": "lodash", "source": "static_scan"}]}))
    db.commit()
    doc = json.loads(render_npm_package_json(db))
    assert doc["dependencies"]["pptxgenjs"] == "^3.0"
    assert doc["dependencies"]["lodash"] == "*"  # bare → "*"
    assert doc["private"] is True


def test_apt_packages(db):
    db.add(_mk("a", {"apt": [{"name": "pandoc", "source": "apt-requirements.txt"}]}))
    db.add(_mk("b", {"apt": [{"name": "libreoffice", "source": "manual"}]}))
    db.commit()
    apt = render_apt_packages(db)
    assert "pandoc" in apt and "libreoffice" in apt
    # Sorted
    lines = [l for l in apt.splitlines() if l and not l.startswith("#")]
    assert lines == sorted(lines)


def test_hash_changes_on_change(db):
    db.add(_mk("a", {"pip": [{"name": "pandas", "source": "manual"}]}))
    db.commit()
    h1 = aggregate_all(db)["hash"]
    db.query(AdminSkill).filter(AdminSkill.skill_id == "a").update(
        {"dependencies": {"pip": [{"name": "numpy", "source": "manual"}]}}
    )
    db.commit()
    h2 = aggregate_all(db)["hash"]
    assert h1 != h2


def test_write_manifests_creates_and_tracks_changes(db, tmp_path):
    db.add(_mk("a", {"pip": [{"name": "pandas", "source": "manual"}]}))
    db.commit()
    info = write_manifests(db, repo_root=str(tmp_path))
    assert (tmp_path / "requirements-skills.txt").exists()
    assert (tmp_path / "package-skills.json").exists()
    assert (tmp_path / "apt-skills.txt").exists()
    assert "requirements-skills.txt" in info["changed"]

    # Second write with no DB change → no files changed
    info2 = write_manifests(db, repo_root=str(tmp_path))
    assert info2["changed"] == []
    assert info2["hash"] == info["hash"]


def test_write_manifests_repo_root_missing(db, tmp_path):
    with pytest.raises(FileNotFoundError):
        write_manifests(db, repo_root=str(tmp_path / "does_not_exist"))
