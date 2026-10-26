"""Renewed deadlines survive controller recreation and durable-write failures."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.sandbox.test_opensandbox_controller_patch import SOURCE, extract, patch

spec = importlib.util.spec_from_file_location("expirations", Path(__file__).resolve().parents[4] / "docker/opensandbox-server/opensandbox_expirations.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

requires_source = pytest.mark.skipif(
    not SOURCE.is_dir(), reason="Set OPENSANDBOX_TEST_SOURCE to the pinned server source")


def test_renewed_deadline_survives_store_recreation(tmp_path):
    path = tmp_path / "controller" / "leases.sqlite3"
    store = module.ExpirationStore(path)
    store.put("sandbox", "2026-10-18T13:00:00+00:00")
    store.put("sandbox", "2026-10-19T13:00:00+00:00")
    restarted = module.ExpirationStore(path)
    assert restarted.get("sandbox") == "2026-10-19T13:00:00+00:00"
    restarted.delete("sandbox")
    assert module.ExpirationStore(path).get("sandbox") is None


@requires_source
def test_failed_persistence_retains_existing_timer_and_deadline():
    source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
    existing = Mock()
    service = SimpleNamespace(_expiration_lock=Lock(),
        _expiration_store=SimpleNamespace(put=Mock(side_effect=OSError("disk full"))),
        _expiration_timers={"sandbox": existing}, _sandbox_expirations={"sandbox": "original"},
        _expire_sandbox=Mock())
    timer = Mock()
    schedule = extract(source, "_schedule_expiration", {
        "Timer": Mock(return_value=timer), "datetime": datetime, "timezone": timezone})
    with pytest.raises(OSError, match="disk full"):
        schedule(service, "sandbox", datetime(2099, 1, 1, tzinfo=timezone.utc))
    existing.cancel.assert_not_called()
    timer.start.assert_not_called()
    assert service._sandbox_expirations["sandbox"] == "original"


@requires_source
def test_restart_uses_durable_deadline_instead_of_immutable_creation_label(tmp_path):
    from unittest.mock import Mock
    import json
    from fastapi import HTTPException
    source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
    store = module.ExpirationStore(tmp_path / "leases.sqlite3")
    store.put("sandbox", "2099-01-01T00:00:00+00:00")
    container = SimpleNamespace(attrs={"Config": {"Labels": {
        "id": "sandbox", "expires": "2000-01-01T00:00:00+00:00"}}})
    service = SimpleNamespace(_owned_runtime_containers=lambda: [container],
        _expiration_store=store, _ossfs_mount_lock=Lock(),
        _schedule_expiration=Mock(), _expire_sandbox=Mock())
    restore = extract(source, "_restore_existing_sandboxes", {
        "DockerException": RuntimeError, "logger": Mock(), "datetime": datetime,
        "timezone": timezone, "SANDBOX_ID_LABEL": "id", "EGRESS_SIDECAR_LABEL": "sidecar",
        "SANDBOX_OSSFS_MOUNTS_LABEL": "mounts", "SANDBOX_EXPIRES_AT_LABEL": "expires",
        "parse_timestamp": datetime.fromisoformat, "json": json, "HTTPException": HTTPException})
    restore(service)
    service._schedule_expiration.assert_called_once_with("sandbox", datetime(2099, 1, 1, tzinfo=timezone.utc))
    service._expire_sandbox.assert_not_called()


@requires_source
def test_delete_store_failure_is_reported_and_does_not_block_resource_cleanup():
    source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
    timer = Mock()
    logger = Mock()
    service = SimpleNamespace(_expiration_lock=Lock(),
        _expiration_store=SimpleNamespace(delete=Mock(side_effect=OSError("disk full"))),
        _expiration_timers={"sandbox": timer}, _sandbox_expirations={"sandbox": "deadline"})
    remove = extract(source, "_remove_expiration_tracking", {"logger": logger})
    remove(service, "sandbox")
    timer.cancel.assert_called_once()
    assert service._sandbox_expirations == {} and service._expiration_timers == {}
    logger.exception.assert_called_once()


@requires_source
@pytest.mark.parametrize("failed_step", ["lease", "mount"])
def test_failed_initial_lease_rolls_back_container_and_mounts(failed_step):
    source = patch.patched_sources(SOURCE)["services/docker/docker_service.py"]
    box, sidecar = Mock(), Mock()
    service = Mock()
    service.network_mode = "bridge"
    service._build_labels_and_env.return_value = ({}, {})
    service._resolve_image_auth.return_value = ("image", None)
    service._resolve_resource_limits.return_value = (None, None, None)
    service._prepare_ossfs_mounts.return_value = ["mount"]
    service._build_volume_binds.return_value = {}
    service._base_host_config_kwargs.return_value = {}
    service._start_egress_sidecar.return_value = sidecar
    service._create_and_start_container.return_value = box
    if failed_step == "lease":
        service._schedule_expiration.side_effect = OSError("disk full")
    request = SimpleNamespace(platform=None, volumes=[], network_policy=True,
        resource_limits=SimpleNamespace(root={}), entrypoint=["sh"])
    namespace = {"json": __import__("json"), "is_windows_platform": lambda p: False,
        "generate_egress_token": lambda: "token", "allocate_port_bindings": lambda p: {"44772": ("", 1), "8080": ("", 2)},
        "DockerException": RuntimeError, "logger": Mock(), "remove_helper": Mock(), "initialize_mounts": Mock(),
        "MANIFEST": "manifest", "mount_manifest": lambda volumes: []}
    for name in ["SANDBOX_MANAGED_VOLUMES_LABEL", "SANDBOX_OSSFS_MOUNTS_LABEL",
                 "SANDBOX_EGRESS_AUTH_TOKEN_METADATA_KEY", "SANDBOX_EMBEDDING_PROXY_PORT_LABEL", "SANDBOX_HTTP_PORT_LABEL"]:
        namespace[name] = name
    if failed_step == "mount":
        namespace["initialize_mounts"].side_effect = OSError("disk full")
    provision = extract(source, "_provision_sandbox", namespace)
    with pytest.raises(OSError, match="disk full"):
        provision(service, "sandbox", request, datetime.now(timezone.utc), datetime(2099,1,1,tzinfo=timezone.utc))
    box.remove.assert_called_once_with(force=True)
    sidecar.remove.assert_called_once_with(force=True)
    service._release_ossfs_mounts.assert_called_once_with([])
    service._cleanup_managed_volumes.assert_called_once_with("sandbox", [])
    namespace["remove_helper"].assert_called_once_with(service, "sandbox")
