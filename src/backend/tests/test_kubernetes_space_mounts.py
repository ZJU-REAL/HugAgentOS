"""Controlled mount workload boundaries and upstream patch checks."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("space_mounts", ROOT / "deploy_kit/kubernetes/space_mounts.py")
mounts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mounts)

@pytest.fixture
def config(monkeypatch):
    values = {"OPENSANDBOX_SPACE_MOUNT_IMAGE": "test/helper:1",
              "HUGAGENT_SPACE_SECRET_NAME": "space-storage",
              "HUGAGENT_SPACE_ENDPOINT": "http://minio:9000",
              "HUGAGENT_SPACE_BUCKET": "spaces", "HUGAGENT_SPACE_PREFIX": "prod"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values

def volume(path="/workspace/myspace/alice", prefix="prod/spaces/users/alice/files", readonly=False):
    return SimpleNamespace(ossfs=SimpleNamespace(bucket="spaces", endpoint="http://minio:9000",
        access_key_id="server-managed", access_key_secret="server-managed", options=None),
        mount_path=path, sub_path=prefix, read_only=readonly)

def test_mount_isolated_credentials_and_kernel_propagation(config):
    pod = {"containers": [{"name": "main", "securityContext": {"privileged": False}}]}
    assert mounts.apply_object_volumes(pod, [volume(readonly=True)]) == []
    main = pod["containers"][0]
    helper = pod["initContainers"][0]
    assert helper["restartPolicy"] == "Always"
    assert main["securityContext"] == {"privileged": False}
    assert main["volumeMounts"][0]["mountPropagation"] == "HostToContainer"
    assert helper["volumeMounts"][0]["mountPropagation"] == "Bidirectional"
    assert all(v["name"] != "hugagent-space-credentials" for v in main["volumeMounts"])
    assert helper["securityContext"]["privileged"]
    assert not pod["shareProcessNamespace"]
    assert not pod["automountServiceAccountToken"]
    assert all("hostPath" not in v and "persistentVolumeClaim" not in v for v in pod["volumes"])
    assert '"read_only": true' in helper["env"][0]["value"]

@pytest.mark.parametrize("prefix", ["prod/spaces/users/alice/../bob", "other/spaces/users/alice/files",
                                     "prod/spaces//files", "prod/spaces/users/alice/files/"])
def test_invalid_prefix_rejected(config, prefix):
    with pytest.raises(ValueError):
        mounts.apply_object_volumes({"containers": [{"name": "main"}]}, [volume(prefix=prefix)])

def test_shared_process_namespace_rejected(config):
    with pytest.raises(ValueError, match="private process"):
        mounts.apply_object_volumes({"shareProcessNamespace": True, "containers": [{}]}, [volume()])

def test_missing_config_fails_closed(config, monkeypatch):
    monkeypatch.delenv("OPENSANDBOX_SPACE_MOUNT_IMAGE")
    with pytest.raises(ValueError, match="incomplete"):
        mounts.apply_object_volumes({"containers": [{}]}, [volume()])

def test_endpoint_cannot_be_overridden(config):
    v = volume()
    v.ossfs.endpoint = "http://attacker"
    with pytest.raises(ValueError, match="fixed server"):
        mounts.apply_object_volumes({"containers": [{}]}, [v])


def test_disconnected_mount_is_detached_before_path_access(monkeypatch):
    helper_spec = importlib.util.spec_from_file_location("space_helper", ROOT / "docker/space-mount.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    state = {"mounted": True}
    class Parent:
        def mkdir(self, **kwargs): pass
        def chmod(self, mode): pass
    class Mount:
        parent = Parent()
        def __str__(self): return "/workspace/myspace/alice"
        def mkdir(self, **kwargs):
            if state["mounted"]:
                raise OSError(107, "Transport endpoint is not connected")
        def chmod(self, mode):
            assert mode == 0
    monkeypatch.setattr(helper, "mounted", lambda path: state["mounted"])
    def detach(command, check):
        assert command == ["fusermount", "-u", "/workspace/myspace/alice"] and check
        state["mounted"] = False
    monkeypatch.setattr(helper.subprocess, "run", detach)
    helper.prepare_mount(Mount())
    assert not state["mounted"]


def test_mount_revision_changes_when_kubernetes_mode_changes(monkeypatch):
    from core.files.spaces.mounts import mount_revision
    monkeypatch.setenv("STORAGE_TYPE", "s3")
    monkeypatch.setenv("DEPLOY_PROFILE", "kubernetes")
    monkeypatch.setenv("HUGAGENT_SPACE_MOUNT_MODE", "csi")
    old = mount_revision([])
    monkeypatch.setenv("HUGAGENT_SPACE_MOUNT_MODE", "sidecar")
    assert mount_revision([]) != old
