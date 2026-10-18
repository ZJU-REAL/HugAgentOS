"""Recover FUSE connections without replacing a sandbox or its workspace."""
import json
import os
from pathlib import PurePosixPath
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from opensandbox_server.api.schema import Volume
from . import sandbox_metadata
from .object_mounts import mount_identity, validated_options

EPOCH = uuid4().hex
REVISION = "hugagent-space-revision"
HELPER_LABEL = "hugagent.mount-sandbox"
_locks = tuple(RLock() for _ in range(64))

class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str
    revision: str
    volumes: list[Volume]

def helper_name(sandbox_id):
    return "sandbox-mount-" + sandbox_id

def _specifications(service, container, request):
    labels = container.attrs["Config"]["Labels"]
    if (labels.get(sandbox_metadata.USER_ID_METADATA_KEY) != request.actor
            or labels.get(REVISION) != request.revision):
        raise HTTPException(409, "Sandbox file authorization changed")
    root = service.app_config.storage.ossfs_mount_root.rstrip("/") + "/"
    mounts = {m["Destination"]: m for m in container.attrs["Mounts"]
              if m.get("Type") == "bind" and m.get("Source", "").startswith(root)}
    volumes = [v for v in request.volumes if v.ossfs is not None]
    if len({v.mount_path for v in volumes}) != len(volumes) or set(mounts) != {v.mount_path for v in volumes}:
        raise HTTPException(409, "Sandbox file mount scope changed")
    specs = []
    for volume in volumes:
        service._validate_ossfs_volume(volume)
        mount = mounts[volume.mount_path]
        # Configuration, credentials, prefix and read-only flags must match the
        # original authorized bind. The generation suffix is connection-local.
        expected = mount_identity(volume)
        actual = PurePosixPath(mount["Source"]).name.split("-")[0]
        if expected != actual or bool(mount["RW"]) == volume.read_only:
            raise HTTPException(409, "Sandbox file mount specification changed")
        specs.append({"target": volume.mount_path,
            "source": volume.ossfs.bucket + ":/" + volume.sub_path,
            "endpoint": volume.ossfs.endpoint,
            "options": validated_options(service, volume),
            "read_only": volume.read_only,
            "credentials": volume.ossfs.bucket + ":" + volume.ossfs.access_key_id
                           + ":" + volume.ossfs.access_key_secret})
    return specs

def _healthy(container, specs):
    if not specs:
        return True
    script = """import json,os,sys
from pathlib import Path
mounts = {}
for line in Path('/proc/self/mountinfo').read_text().splitlines():
    fields = line.split()
    separator = fields.index('-')
    mounts[fields[4]] = (fields[separator + 1], fields[5].split(','))
for target, readonly in json.loads(sys.argv[1]):
    filesystem, options = mounts[target]
    assert filesystem.startswith('fuse'), 'Object filesystem is absent'
    assert not readonly or 'ro' in options, 'Read-only mount is required'
    os.stat(target)
"""
    result = container.exec_run(["timeout", "4", "python3", "-c", script,
                                 json.dumps([(s["target"], s["read_only"]) for s in specs])], user="0")
    return result.exit_code == 0

def remove_helper(service, sandbox_id):
    from docker.errors import NotFound
    try:
        helper = service.docker_client.containers.get(helper_name(sandbox_id))
    except NotFound:
        return
    if helper.labels.get(HELPER_LABEL) != sandbox_id:
        raise RuntimeError("Mount helper ownership mismatch")
    helper.remove(force=True)

def ensure_mounts(service, sandbox_id, request):
    import hashlib
    slot = int(hashlib.sha256(sandbox_id.encode()).hexdigest()[:2], 16) % len(_locks)
    with _locks[slot]:
        container = service._get_container_by_sandbox_id(sandbox_id)
        if not container.attrs["State"]["Running"]:
            raise HTTPException(503, "Sandbox must be running for mount recovery")
        specs = _specifications(service, container, request)
        if _healthy(container, specs):
            return {"epoch": EPOCH, "recovered": False}
        controller = service.docker_client.containers.get(os.environ["HOSTNAME"])
        project = controller.attrs["Config"]["Labels"]["com.docker.compose.project"]
        remove_helper(service, sandbox_id)
        helper = service.docker_client.containers.run(
            controller.image.id, ["sleep", "infinity"], entrypoint=[],
            name=helper_name(sandbox_id), detach=True,
            pid_mode="container:" + container.id,
            network_mode=controller.attrs["HostConfig"]["NetworkMode"],
            cap_drop=["ALL"], cap_add=["SYS_ADMIN", "SYS_PTRACE", "SYS_CHROOT", "DAC_OVERRIDE"],
            devices=["/dev/fuse:/dev/fuse:rwm"],
            security_opt=["apparmor=unconfined", "no-new-privileges:true"],
            read_only=True, tmpfs={"/mounts": "rw,mode=0700", "/run/secrets": "rw,mode=0700",
                                  "/tmp": "rw,mode=1777"},
            mem_limit="256m", pids_limit=128,
            labels={HELPER_LABEL: sandbox_id, "hugagent.project": project},
        )
        try:
            api = service.docker_client.api
            timeout = 30 * len(specs) + 10
            execution = api.exec_create(helper.id, ["timeout", str(timeout), "python",
                "/app/opensandbox_server/services/docker/mount_worker.py"],
                stdin=True, stdout=True, stderr=True, user="0")
            stream = api.exec_start(execution["Id"], socket=True)
            try:
                connection = stream._sock
                connection.settimeout(timeout + 5)
                connection.sendall(json.dumps(specs).encode() + b"\n")
                while connection.recv(4096):
                    pass
            finally:
                stream.close()
            if api.exec_inspect(execution["Id"])["ExitCode"] != 0 or not _healthy(container, specs):
                raise HTTPException(503, "Object mount recovery failed; workspace retained")
        except Exception:
            remove_helper(service, sandbox_id)
            raise
        return {"epoch": EPOCH, "recovered": True}
