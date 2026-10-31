"""Kubernetes skill delivery keeps account mounts and native process admission."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock
import io
import tarfile

import pytest

from core.sandbox.protocol import ProcessRequest
from core.sandbox.providers.opensandbox import creation, skills, connection
from core.sandbox.errors import SandboxError, SandboxConnectError


@pytest.mark.asyncio
async def test_kubernetes_creation_preserves_space_mounts_env_and_resource(monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv("DEPLOY_PROFILE", "kubernetes")
    monkeypatch.setattr(creation, "settings", replace(
        creation.settings, sandbox=replace(creation.settings.sandbox, cpu="750m", memory="1536Mi")))
    monkeypatch.setattr(creation, "_make_skills_volumes", lambda _: pytest.fail("host skill bind requested"))
    monkeypatch.setattr(creation, "credential_mounts", lambda _: [])
    host = creation._OpenSandboxCreationMixin()
    host._make_config = lambda: "native-config"
    host._session_expiry_s = lambda: 600
    mount = object()
    host._additional_workspace_mounts = AsyncMock(return_value=([mount], {"mount-revision": "current"}))
    host._create_owned_sandbox = AsyncMock(return_value=SimpleNamespace(id="native"))
    host._image = "native-image"
    await host._create_jupyter_sandbox(user_id="alice")
    args, kwargs = host._create_owned_sandbox.call_args
    assert args == ("native-image",)
    assert kwargs["volumes"] == [mount]
    assert kwargs["resource"] == {"cpu": "750m", "memory": "1536Mi"}
    assert kwargs["env"]["EXECD_API_GRACE_SHUTDOWN"] == "0s"
    assert kwargs["env"]["JX_MYSPACE_UID"] == "alice"
    assert kwargs["metadata"]["mount-revision"] == "current"
    # Snapshot creation receives the same resources and grants.
    await host._create_jupyter_sandbox(user_id="alice", from_snapshot_id="snapshot")
    assert host._create_owned_sandbox.call_args.kwargs["snapshot_id"] == "snapshot"
    assert host._create_owned_sandbox.call_args.kwargs["resource"] == kwargs["resource"]


@pytest.mark.asyncio
async def test_kubernetes_creation_requires_configured_resources(monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv("DEPLOY_PROFILE", "kubernetes")
    monkeypatch.setattr(creation, "settings", replace(
        creation.settings, sandbox=replace(creation.settings.sandbox, cpu="", memory="1Gi")))
    monkeypatch.setattr(creation, "credential_mounts", lambda _: [])
    host = creation._OpenSandboxCreationMixin()
    host._make_config = lambda: None
    host._session_expiry_s = lambda: 600
    host._additional_workspace_mounts = AsyncMock(return_value=([], {}))
    host._create_owned_sandbox = AsyncMock()
    host._image = "native-image"
    with pytest.raises(SandboxConnectError, match="SANDBOX_CPU and SANDBOX_MEMORY"):
        await host._create_jupyter_sandbox(user_id="alice")
    host._create_owned_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_configured_host_credentials_are_rejected_not_discarded(monkeypatch):
    monkeypatch.setenv("DEPLOY_PROFILE", "kubernetes")
    monkeypatch.setattr(creation, "credential_mounts", lambda _: [SimpleNamespace(host=object())])
    host = creation._OpenSandboxCreationMixin()
    host._make_config = lambda: None
    host._additional_workspace_mounts = AsyncMock(return_value=([], {}))
    with pytest.raises(SandboxConnectError, match="host credential"):
        await host._create_jupyter_sandbox(user_id="alice")


def test_native_endpoint_resolution_keeps_authenticated_server_proxy(monkeypatch):
    host = connection._OpenSandboxConnectionMixin()
    host._domain, host._api_key, host._request_timeout_s = "http://sandbox:8080", "key", 30
    monkeypatch.setenv("DEPLOY_PROFILE", "kubernetes")
    assert host._make_config().use_server_proxy is True
    monkeypatch.delenv("DEPLOY_PROFILE")
    assert host._make_config().use_server_proxy is True


@pytest.fixture
def shared_state(monkeypatch):
    from core.infra import ephemeral
    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    return state


def _sandbox(sandbox_id="sbx-1", write_file=None):
    return SimpleNamespace(id=sandbox_id, files=SimpleNamespace(write_file=write_file or AsyncMock()),
        commands=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(exit_code=0))))


@pytest.fixture
def publication(tmp_path, monkeypatch, shared_state):
    from core.capabilities.skills import publication
    from core.capabilities.catalog import api
    view = tmp_path / "alice"
    view.mkdir()
    monkeypatch.setattr(skills, "_database_visible", lambda names, user: set(names))
    monkeypatch.setattr(publication, "publication_lock", nullcontext)
    monkeypatch.setattr(publication, "prepare_skill_view", lambda user: view if user == "alice" else tmp_path / "other")
    monkeypatch.setattr(api, "get_catalog", lambda **kw: {"skills": [{"id": "disabled", "enabled": False}]})
    package = view / "selected"
    package.mkdir()
    (package / "SKILL.md").write_text("selected instructions")
    (package / "run.sh").write_text("#!/bin/sh\necho selected")
    (package / "run.sh").chmod(0o755)
    private = tmp_path / "other" / "secret"
    private.mkdir(parents=True)
    (private / "SKILL.md").write_text("private")
    return view


def test_view_contains_only_current_authorized_enabled_skill_bytes(publication):
    disabled = publication / "disabled"
    disabled.mkdir()
    (disabled / "SKILL.md").write_text("disabled")
    result = skills._view_archive(skills._view_files("alice"))
    with tarfile.open(fileobj=io.BytesIO(result)) as archive:
        assert sorted(archive.getnames()) == ["selected/SKILL.md", "selected/run.sh"]
        assert archive.getmember("selected/run.sh").mode == 0o755
    (publication / "selected" / "run.sh").write_text("new revision")
    assert skills._view_archive(skills._view_files("alice")) != result


def test_skill_symlinks_are_rejected_before_upload(publication):
    (publication / "selected" / "leak").symlink_to(publication.parent / "other/secret/SKILL.md")
    with pytest.raises(ValueError, match="软链接"):
        skills._view_archive(skills._view_files("alice"))


@pytest.mark.asyncio
async def test_staging_runs_as_user_and_propagates_failure(publication):
    sandbox = _sandbox()
    req = ProcessRequest(script_content="/workspace/skills/selected/run.sh", script_name="test", user_id="alice")
    await skills.stage_skill_view(sandbox, req)
    sandbox.files.write_file.assert_awaited_once()
    call = sandbox.commands.run.call_args_list[0]
    assert str(call.kwargs["opts"].uid) == "1000"
    assert "python3 -c" in call.args[0]
    sandbox.commands.run.side_effect = [SimpleNamespace(exit_code=1), SimpleNamespace(exit_code=0)]
    (publication / "selected" / "SKILL.md").write_text("changed instructions")
    with pytest.raises(SandboxError, match="synchronize authorized"):
        await skills.stage_skill_view(sandbox, req)
    # A failed transfer is never remembered as installed.
    sandbox.commands.run.side_effect = None
    await skills.stage_skill_view(sandbox, req)
    assert sandbox.files.write_file.await_count == 3


@pytest.mark.asyncio
async def test_unchanged_view_is_not_retransferred_but_changes_and_resets_are(publication):
    sandbox = _sandbox()
    req = ProcessRequest(script_content="echo test", script_name="test", user_id="alice")
    await skills.stage_skill_view(sandbox, req)
    await skills.stage_skill_view(sandbox, req)
    assert sandbox.files.write_file.await_count == 1
    assert sandbox.commands.run.await_count == 2  # one sync plus its cleanup
    # Revocation is still evaluated for every command.
    (publication / "selected" / "run.sh").write_text("#!/bin/sh\necho revised")
    await skills.stage_skill_view(sandbox, req)
    assert sandbox.files.write_file.await_count == 2
    # A sandbox reset for another session receives the view again.
    await skills.forget_skill_view(sandbox)
    await skills.stage_skill_view(sandbox, req)
    assert sandbox.files.write_file.await_count == 3


@pytest.mark.asyncio
async def test_revocation_applies_when_another_worker_installed_the_newer_view(publication):
    req = ProcessRequest(script_content="echo test", script_name="test", user_id="alice")
    worker_a, worker_b = _sandbox(), _sandbox()  # two processes attached to one sandbox
    await skills.stage_skill_view(worker_a, req)
    extra = publication / "granted"
    extra.mkdir()
    (extra / "SKILL.md").write_text("granted later")
    await skills.stage_skill_view(worker_b, req)
    # Revoke it again; worker A's last view equals the current one, but the
    # sandbox holds worker B's newer view, so A must transfer.
    import shutil
    shutil.rmtree(extra)
    await skills.stage_skill_view(worker_a, req)
    assert worker_a.files.write_file.await_count == 2


@pytest.mark.asyncio
async def test_failed_transfer_leaves_no_installed_record(publication, shared_state):
    sandbox = _sandbox()
    sandbox.commands.run.side_effect = [SimpleNamespace(exit_code=1), SimpleNamespace(exit_code=0)]
    req = ProcessRequest(script_content="echo test", script_name="test", user_id="alice")
    with pytest.raises(SandboxError):
        await skills.stage_skill_view(sandbox, req)
    assert await shared_state.get(skills._installed_key(sandbox)) is None


@pytest.mark.asyncio
async def test_concurrent_staging_is_serialized_per_sandbox(publication):
    import asyncio
    active, peak = 0, 0

    async def write_file(path, content):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    sandbox = _sandbox(write_file=write_file)
    req = ProcessRequest(script_content="echo test", script_name="test", user_id="alice")
    await asyncio.gather(*(skills.stage_skill_view(sandbox, req) for _ in range(3)))
    assert peak == 1


@pytest.mark.asyncio
async def test_command_staging_keeps_api_packages_and_docker_mounts(monkeypatch):
    from core.sandbox.providers.opensandbox.execution import _OpenSandboxExecMixin
    stage = AsyncMock()
    monkeypatch.setattr(skills, "stage_skill_view", stage)
    host = _OpenSandboxExecMixin()
    host._persistent_setup = AsyncMock()
    host._sync_inputs_to_sandbox = AsyncMock()
    session = SimpleNamespace(sandbox=object())
    request = ProcessRequest(script_content="echo test", script_name="test", user_id="alice")
    monkeypatch.setenv("DEPLOY_PROFILE", "kubernetes")
    await host._sync_inputs(session, request)
    stage.assert_awaited_once_with(session.sandbox, request)
    request.user_id = "api_ak_123"
    await host._sync_inputs(session, request)
    assert stage.await_count == 1
    monkeypatch.delenv("DEPLOY_PROFILE")
    request.user_id = "alice"
    await host._sync_inputs(session, request)
    assert stage.await_count == 1
    assert host._sync_inputs_to_sandbox.await_count == 3


def test_relative_execution_and_revocation_preserve_unchanged_cwd(publication, tmp_path, monkeypatch):
    import subprocess
    import sys
    from core.capabilities.catalog import api
    root = tmp_path / "sandbox" / "skills"
    root.parent.mkdir()
    script = skills._SYNC_VIEW.replace("Path('/workspace/skills')", "Path(" + repr(str(root)) + ")")
    archive = tmp_path / "view.tgz"

    def synchronize():
        archive.write_bytes(skills._view_archive(skills._view_files("alice")))
        subprocess.run([sys.executable, "-c", script, str(archive)], check=True)

    synchronize()
    assert subprocess.check_output(["sh", "selected/run.sh"], cwd=root, text=True).strip() == "selected"
    inode = (root / "selected").stat().st_ino
    synchronize()
    assert (root / "selected").stat().st_ino == inode
    # Relative references see the current revision, not the first copy.
    (publication / "selected" / "run.sh").write_text("#!/bin/sh\necho updated")
    synchronize()
    assert subprocess.check_output(["sh", "selected/run.sh"], cwd=root, text=True).strip() == "updated"
    # Even a command with no skill path now synchronizes an empty/revoked view.
    monkeypatch.setattr(api, "get_catalog", lambda **kw: {"skills": [{"id": "selected", "enabled": False}]})
    synchronize()
    assert not (root / "selected").exists()
    assert subprocess.run(["sh", "selected/run.sh"], cwd=root, capture_output=True).returncode != 0


def test_deleted_package_removed_and_sandbox_symlink_not_followed(publication, tmp_path):
    import subprocess
    import sys
    import shutil
    root = tmp_path / "sandbox-skills"
    root.mkdir()
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (root / "stale-link").symlink_to(outside)
    (root / "deleted").mkdir()
    (root / "deleted" / "private").write_text("removed")
    shutil.rmtree(publication / "selected")
    archive = tmp_path / "empty.tgz"
    archive.write_bytes(skills._view_archive(skills._view_files("alice")))
    script = skills._SYNC_VIEW.replace("Path('/workspace/skills')", "Path(" + repr(str(root)) + ")")
    subprocess.run([sys.executable, "-c", script, str(archive)], check=True)
    assert list(root.iterdir()) == []
    assert (outside / "keep").read_text() == "keep"


def test_database_enablement_owner_and_dependency_are_checked_without_cache(db_session, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from core.db import engine
    from core.db.models import AdminSkill
    monkeypatch.setattr(engine, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    row = AdminSkill(skill_id="view-native-test", display_name="view", description="test", skill_content="test",
                     is_enabled=True, owner_user_id="alice", dep_status="ready")
    db_session.add(row)
    db_session.commit()
    assert skills._database_visible([row.skill_id], "alice") == {row.skill_id}
    assert skills._database_visible([row.skill_id], "bob") == set()
    row.is_enabled = False
    db_session.commit()
    assert skills._database_visible([row.skill_id], "alice") == set()
    row.is_enabled, row.dep_status = True, "pending"
    db_session.commit()
    assert skills._database_visible([row.skill_id], "alice") == set()


def test_kubernetes_rejects_desktop_cached_publication_instead_of_reusing_it(monkeypatch):
    from core.harness.registry import run_capabilities
    monkeypatch.setattr(run_capabilities, "has_run_capabilities", lambda: True)
    with pytest.raises(SandboxError, match="desktop capability publication"):
        skills._view_files("alice")
