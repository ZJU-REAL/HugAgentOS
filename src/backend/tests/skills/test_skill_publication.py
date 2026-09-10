"""Regression coverage at skill loading, publication and sandbox preparation boundaries."""

from pathlib import Path

from core.agent_skills.backends.composite import CompositeBackend
from core.agent_skills.backends.protocol import SkillFileInfo
from core.agent_skills.loader import MultiSourceSkillLoader


class MutableSource:
    source_name = "admin"
    priority = 75

    def __init__(self):
        self.files = {"scripts/retired.py": "old", "scripts/run.py": "version one"}

    def list_skill_files(self):
        return [
            SkillFileInfo(
                "sample",
                Path("/db/sample/SKILL.md"),
                "admin",
                75,
                metadata={"owner_user_id": "alice"},
                is_database=True,
            )
        ]

    def read_skill_file(self, skill_id):
        return "---\nname: sample\ndescription: example\n---\nRun"

    def get_extra_files(self, skill_id):
        return dict(self.files)

    def exists(self, skill_id):
        return True


def test_replacement_publishes_exact_file_set_with_fresh_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)
    source = MutableSource()
    first = MultiSourceSkillLoader(CompositeBackend([source]))
    first.get_skill_base_dir("sample")
    source.files = {"scripts/run.py": "version two", "assets/new.txt": "new"}
    second = MultiSourceSkillLoader(CompositeBackend([source]))
    path = Path(second.get_skill_base_dir("sample"))
    assert {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()} == {
        "SKILL.md",
        "scripts/run.py",
        "assets/new.txt",
    }
    assert (path / "scripts/run.py").read_text() == "version two"


import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    import core.db.engine as dbe
    from core.agent_skills import config, loader
    from api.routes.v1 import me_capabilities, admin_skills
    from api.deps import require_admin

    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)
    builtin = tmp_path / "bundles"
    builtin.mkdir()
    monkeypatch.setattr(
        config,
        "get_default_skill_sources",
        lambda: [
            config.SkillSourceConfig("built-in", builtin, 0),
            config.SkillSourceConfig("admin", tmp_path / "unused", 75),
        ],
    )
    monkeypatch.setattr(config, "_builtin_skills_dir", lambda: builtin)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'db.sqlite'}", connect_args={"check_same_thread": False}
    )
    dbe.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbe, "SessionLocal", factory)
    monkeypatch.setattr(loader, "_global_loader", None)
    monkeypatch.setattr(
        me_capabilities, "resolve_user_capabilities", lambda *a: {"can_add_skill": True}
    )

    def session():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(me_capabilities.router)
    app.include_router(admin_skills.router)
    app.dependency_overrides[dbe.get_db] = session
    app.dependency_overrides[me_capabilities.get_current_user] = lambda: SimpleNamespace(
        user_id="alice"
    )
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, factory=factory, builtin=builtin, storage=tmp_path / "skills"
        )
    loader._global_loader = None
    engine.dispose()


def upload(client, files, prefix="/v1/me/skills/upload"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: sample\ndescription: example\n---\nRun")
        for name, data in files.items():
            archive.writestr(name, data)
    response = client.post(
        prefix, files={"file": ("skill.zip", buffer.getvalue(), "application/zip")}
    )
    assert response.status_code in (200, 201), response.text
    return response


def test_web_delete_removes_materialized_skill(cloud):
    from core.agent_skills.loader import get_skill_loader

    upload(cloud.client, {"old.txt": "old"})
    path = Path(get_skill_loader().get_skill_base_dir("sample"))
    response = cloud.client.delete("/v1/me/skills/sample")
    assert response.status_code == 200, response.text
    assert not path.exists()


def test_upload_replace_and_new_sandbox_view_are_exact(cloud):
    from core.agent_skills.publication import prepare_skill_view

    upload(cloud.client, {"old.txt": "old", "nested/run.txt": "v1"})
    view = prepare_skill_view("alice")
    assert (view / "sample/old.txt").exists()
    upload(cloud.client, {"nested/run.txt": "v2", "nested/data.bin": b"\x00\xff"})
    view = prepare_skill_view("alice")
    tree = view / "sample"
    assert {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()} == {
        "SKILL.md",
        "nested/run.txt",
        "nested/data.bin",
    }
    assert (tree / "nested/run.txt").read_text() == "v2"
    assert (tree / "nested/data.bin").read_bytes() == b"\x00\xff"


def test_old_loader_cannot_republish_deleted_or_replaced_payload(cloud):
    from core.agent_skills.loader import get_skill_loader

    upload(cloud.client, {"old.txt": "v1"})
    stale = get_skill_loader()
    stale.get_skill_base_dir("sample")
    upload(cloud.client, {"new.txt": "v2"})
    path = Path(stale.get_skill_base_dir("sample"))
    assert not (path / "old.txt").exists()
    assert (path / "new.txt").read_text() == "v2"
    cloud.client.delete("/v1/me/skills/sample")
    # A loader which still has the ID in its metadata cannot write a zombie.
    try:
        result = stale.get_skill_base_dir("sample")
    except FileNotFoundError:
        result = None
    assert result is None
    assert not path.exists()


def test_new_mounts_exclude_deleted_global_private_and_historical_dirs(cloud, monkeypatch):
    from core.db.models import AdminSkill
    from core.agent_skills.publication import prepare_skill_view
    from core.agent_skills.loader import get_skill_loader
    from core.sandbox._opensandbox_internals import _make_skills_volumes
    from core.config.settings import settings

    upload(cloud.client, {"nested/old.txt": "old"})
    with cloud.factory() as db:
        db.add(
            AdminSkill(
                skill_id="shared",
                display_name="Shared",
                description="Example",
                skill_content="shared",
                extra_files={"x.txt": "public"},
                is_enabled=True,
            )
        )
        db.add(
            AdminSkill(
                skill_id="bob-only",
                display_name="Private",
                description="Example",
                owner_user_id="bob",
                skill_content="private",
                extra_files={"secret.txt": "private"},
                is_enabled=True,
            )
        )
        db.commit()
    # Simulate the old global layout and an orphan which predates the fix.
    for name in ("bob-only", "deleted-before-upgrade"):
        path = cloud.storage / name
        path.mkdir(parents=True)
        (path / "old.txt").write_text("must not be mounted")
    prepare_skill_view("alice")
    cloud.client.delete("/v1/me/skills/sample")
    from dataclasses import replace
    import core.sandbox._opensandbox_internals as internals

    monkeypatch.setattr(
        internals,
        "settings",
        SimpleNamespace(
            sandbox=replace(
                settings.sandbox, opensandbox_host_storage_path=str(cloud.storage.parent)
            )
        ),
    )
    volumes = _make_skills_volumes("alice")
    assert len(volumes) == 2
    assert all(volume.read_only for volume in volumes)
    by_mount = {v.mount_path: Path(v.host.path) for v in volumes}
    assert {p.name for p in by_mount["/workspace/skills"].iterdir()} == {"shared"}
    assert {p.name for p in by_mount["/workspace/skills_shared"].iterdir()} == {"shared"}
    assert (by_mount["/workspace/skills"] / "shared/x.txt").read_text() == "public"


def test_shipped_package_replacement_removes_retired_files(cloud):
    from core.agent_skills.publication import prepare_skill_view

    bundle = cloud.builtin / "bundled"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("---\nname: bundled\ndescription: sample\n---\nrun")
    (bundle / "old.txt").write_text("old")
    view = prepare_skill_view("alice")
    assert (view / "bundled/old.txt").exists()
    (bundle / "old.txt").unlink()
    (bundle / "new.txt").write_text("new")
    view = prepare_skill_view("alice")
    assert not (view / "bundled/old.txt").exists()
    assert (view / "bundled/new.txt").read_text() == "new"


def test_mount_preparation_fails_if_authoritative_source_unavailable(cloud, monkeypatch):
    from core.agent_skills.backends.database import DatabaseBackend
    from core.sandbox._opensandbox_internals import _make_skills_volumes
    from core.config.settings import settings

    def unavailable(self):
        raise ConnectionError("test database unavailable")

    from dataclasses import replace
    import core.sandbox._opensandbox_internals as internals

    monkeypatch.setattr(
        internals,
        "settings",
        SimpleNamespace(
            sandbox=replace(
                settings.sandbox, opensandbox_host_storage_path=str(cloud.storage.parent)
            )
        ),
    )
    monkeypatch.setattr(DatabaseBackend, "list_skill_files", unavailable)
    with pytest.raises(ConnectionError):
        _make_skills_volumes("alice")


def test_single_file_delete_propagates_to_new_view(cloud):
    from core.agent_skills.publication import prepare_skill_view

    upload(cloud.client, {"nested/old.txt": "old", "nested/new.txt": "new"})
    prepare_skill_view("alice")
    response = cloud.client.delete("/v1/me/skills/sample/files/nested/old.txt")
    assert response.status_code == 200, response.text
    path = prepare_skill_view("alice") / "sample"
    assert not (path / "nested/old.txt").exists()
    assert (path / "nested/new.txt").read_text() == "new"


def test_invalid_replacement_preserves_previous_tree_and_cannot_escape(tmp_path, monkeypatch):
    from core.agent_skills.publication import publish_tree

    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "skills"))
    path = tmp_path / "skills/sample"
    publish_tree(path, {"SKILL.md": b"v1", "old.txt": b"old"})
    with pytest.raises(ValueError):
        publish_tree(path, {"SKILL.md": b"v2", "../escaped": b"bad"})
    assert (path / "SKILL.md").read_bytes() == b"v1"
    assert not (tmp_path / "skills/escaped").exists()


def test_failed_swap_preserves_previous_tree(tmp_path, monkeypatch):
    import os
    from core.agent_skills.publication import publish_tree

    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "skills"))
    path = tmp_path / "skills/sample"
    publish_tree(path, {"SKILL.md": b"v1"})
    replace = os.replace

    def fail_stage(src, dst):
        if Path(src).name == "new":
            raise OSError("injected rename failure")
        return replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_stage)
    with pytest.raises(OSError):
        publish_tree(path, {"SKILL.md": b"v2"})
    assert (path / "SKILL.md").read_bytes() == b"v1"


def test_archive_for_new_sandbox_uses_current_files_without_local_cache_reset(
    tmp_path, monkeypatch
):
    import tarfile
    from core.agent_skills.skill_archive import build_skill_tar

    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "skills"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.txt").write_text("old")
    old_archive = build_skill_tar("sample", source)
    (source / "old.txt").unlink()
    (source / "new.txt").write_text("new")
    archive = build_skill_tar("sample", source)
    with tarfile.open(archive) as tf:
        names = {n.removeprefix("./") for n in tf.getnames() if n not in (".", "./")}
        assert names == {"new.txt"}
        assert tf.extractfile(next(m for m in tf.getmembers() if m.isfile())).read() == b"new"
    assert old_archive != archive


def test_startup_respects_db_override_and_bundle_executable_modes(cloud):
    from core.db.models import AdminSkill
    from core.agent_skills.config import sync_builtin_skills_to_sandbox_dir

    bundle = cloud.builtin / "bundled"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("---\nname: bundled\ndescription: sample\n---\nrun")
    script = bundle / "run.sh"
    script.write_text("#!/bin/sh\necho bundled")
    script.chmod(0o755)
    sync_builtin_skills_to_sandbox_dir()
    assert (cloud.storage / "bundled/run.sh").stat().st_mode & 0o777 == 0o755
    with cloud.factory() as db:
        db.add(
            AdminSkill(
                skill_id="bundled",
                display_name="Override",
                description="Example",
                skill_content="overridden",
                extra_files={"current.txt": "current"},
                is_enabled=True,
            )
        )
        db.commit()
    sync_builtin_skills_to_sandbox_dir()
    assert not (cloud.storage / "bundled/run.sh").exists()
    assert (cloud.storage / "bundled/current.txt").read_text() == "current"


def test_admin_delete_and_file_delete_use_the_same_publication_rules(cloud):
    from core.agent_skills.publication import prepare_skill_view

    upload(cloud.client, {"old.txt": "old", "new.txt": "new"})
    response = cloud.client.delete("/v1/admin/skills/sample/files/old.txt")
    assert response.status_code == 200, response.text
    view = prepare_skill_view("alice")
    assert not (view / "sample/old.txt").exists()
    response = cloud.client.delete("/v1/admin/skills/sample")
    assert response.status_code == 200, response.text
    assert not (prepare_skill_view("alice") / "sample").exists()


def test_prepared_view_repairs_stale_shared_link(cloud):
    from core.agent_skills.publication import prepare_skill_view
    from core.agent_skills.config import get_user_skills_root, SHARED_LINK_NAME

    link = get_user_skills_root() / SHARED_LINK_NAME
    link.unlink()
    old = cloud.storage.parent / "retired"
    old.mkdir()
    link.symlink_to(old, target_is_directory=True)
    prepare_skill_view("alice")
    assert link.resolve() == cloud.storage


@pytest.mark.asyncio
async def test_prewarmed_session_delivery_reconciles_before_pool_hit(cloud, monkeypatch):
    from core.agent_skills.publication import prepare_skill_view
    import core.sandbox._opensandbox_session as sessions
    from unittest.mock import AsyncMock

    upload(cloud.client, {"old.txt": "old"})
    view = prepare_skill_view("alice")
    upload(cloud.client, {"new.txt": "new"})
    # Existing pooled mount still points to the same parent, with v1 until delivery.
    assert (view / "sample/old.txt").exists()
    module_class = next(
        value
        for value in vars(sessions).values()
        if isinstance(value, type) and "_create_session_for" in value.__dict__
    )
    fake = SimpleNamespace(
        _lookup_snapshot=AsyncMock(return_value=None),
        _jupyter_user_pool=SimpleNamespace(has_idle=lambda user: True),
        _create_session=AsyncMock(
            return_value=SimpleNamespace(sandbox=SimpleNamespace(id="pooled"))
        ),
    )
    monkeypatch.setattr(sessions, "_user_bound_sandbox_required", lambda: True)
    await module_class._create_session_for(fake, "new-chat", user_id="alice")
    assert fake._create_session.await_count == 1
    assert not (view / "sample/old.txt").exists()
    assert (view / "sample/new.txt").read_text() == "new"


def test_publication_lock_serializes_processes(tmp_path, monkeypatch):
    import multiprocessing
    from core.agent_skills.publication import publication_lock

    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "skills"))
    ctx = multiprocessing.get_context("fork")
    started, acquired = ctx.Event(), ctx.Event()

    def writer():
        started.set()
        with publication_lock():
            acquired.set()

    # Start the worker before acquiring the lock: no inherited lock ownership.
    ready, release = ctx.Event(), ctx.Event()

    def waiting_writer():
        ready.set()
        release.wait(5)
        writer()

    process = ctx.Process(target=waiting_writer)
    process.start()
    assert ready.wait(5)
    try:
        with publication_lock():
            release.set()
            assert started.wait(5)
            assert not acquired.wait(0.1)
        assert acquired.wait(5)
    finally:
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
    assert process.exitcode == 0


def test_view_link_creation_failure_prevents_sandbox_preparation(cloud, monkeypatch):
    from core.agent_skills.publication import prepare_skill_view

    bundle = cloud.builtin / "bundled"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("---\nname: bundled\ndescription: sample\n---\nrun")
    prepare_skill_view("alice")
    original = Path.symlink_to

    def fail(path, target, **kwargs):
        if path.name == "bundled":
            raise PermissionError("injected link failure")
        return original(path, target, **kwargs)

    monkeypatch.setattr(Path, "symlink_to", fail)
    with pytest.raises(PermissionError):
        prepare_skill_view("alice")


def test_unwritable_explicit_skill_root_never_falls_back(tmp_path, monkeypatch):
    from core.agent_skills.config import get_sandbox_skills_dir

    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory")
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(occupied / "skills"))
    with pytest.raises(OSError):
        get_sandbox_skills_dir()


def test_private_collision_does_not_remove_other_users_builtin(cloud):
    from core.agent_skills.publication import prepare_skill_view

    package = cloud.builtin / "sample"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: sample\ndescription: bundled\n---\nBundled")
    (package / "public.txt").write_text("public")
    upload(cloud.client, {"private.txt": "alice"})
    alice = prepare_skill_view("alice")
    assert (alice / "sample/private.txt").read_text() == "alice"
    bob = prepare_skill_view("bob")
    assert (bob / "sample/public.txt").read_text() == "public"
    assert not (bob / "sample/private.txt").exists()
    assert (alice / "sample/private.txt").read_text() == "alice"
    cloud.client.delete("/v1/me/skills/sample")
    alice = prepare_skill_view("alice")
    assert (alice / "sample/public.txt").read_text() == "public"


def test_cube_current_archive_rechecks_owner_and_freezes_bytes(cloud):
    import tarfile
    from core.db.models import AdminSkill
    from core.agent_skills.loader import get_skill_loader
    from core.agent_skills.skill_archive import build_current_skill_tar

    upload(cloud.client, {"public.txt": "v1"})
    with cloud.factory() as db:
        db.query(AdminSkill).filter_by(skill_id="sample").one().owner_user_id = None
        db.commit()
    stale = get_skill_loader(reset=True)
    assert stale.get_skill_owner("sample") is None
    archive = build_current_skill_tar("sample", "alice")
    with cloud.factory() as db:
        skill = db.query(AdminSkill).filter_by(skill_id="sample").one()
        skill.owner_user_id = "bob"
        skill.extra_files = {"private.txt": "bob secret"}
        db.commit()
    assert build_current_skill_tar("sample", "alice") is None
    assert build_current_skill_tar("sample") is None
    private = build_current_skill_tar("sample", "bob")
    with tarfile.open(private) as tar:
        assert tar.extractfile("private.txt").read() == b"bob secret"
        assert "public.txt" not in tar.getnames()
    with tarfile.open(archive) as tar:
        assert tar.extractfile("public.txt").read() == b"v1"
        assert "private.txt" not in tar.getnames()


@pytest.mark.asyncio
async def test_cube_prepush_uses_users_private_override(cloud, monkeypatch):
    import tarfile
    from unittest.mock import AsyncMock
    from core.sandbox.cube_provider import CubeSandboxProvider

    package = cloud.builtin / "sample"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: sample\ndescription: bundled\n---\nBundled")
    (package / "old.txt").write_text("public")
    upload(cloud.client, {"new.txt": "alice"})
    provider = CubeSandboxProvider.__new__(CubeSandboxProvider)
    provider._materialized_skills = {}
    provider._skill_prepush_concurrency = 1
    provider._skill_prepush_max_bytes = 0
    provider._push_skill_archive = AsyncMock()
    monkeypatch.setattr("core.config.catalog.get_enabled_ids", lambda kind: {"sample"})
    sbx = SimpleNamespace(sandbox_id="fresh")
    await provider._prepush_skills(sbx, "alice")
    archive = provider._push_skill_archive.await_args.args[2]
    with tarfile.open(archive) as tar:
        assert tar.extractfile("new.txt").read() == b"alice"
        assert "old.txt" not in tar.getnames()
    await provider._materialize_referenced_skills(
        sbx, "cat /workspace/skills/sample/new.txt", "alice"
    )
    assert provider._push_skill_archive.await_count == 1
