"""Kubernetes: a privileged helper container mounts object storage into the sandbox Pod."""
import asyncio
import json
import os
import shlex
from datetime import timedelta

from fastapi import HTTPException

from core.files.spaces.leases import async_space_lock
from core.files.spaces.mounts import MOUNT_REVISION_TAG, prepare_volumes
from core.files.spaces.transport.k8s_mount import KubernetesMountTransport

_CHECK_SCRIPT = """import json,os
from pathlib import Path
expected=json.loads(EXPECTED)
mounts={}
for line in Path('/proc/self/mountinfo').read_text().splitlines():
 before,after=line.split(' - ',1)
 fields=before.split()
 if after.split()[0]=='fuse.s3fs':
  mounts[fields[4]]=fields[5].split(',')
for path,readonly in expected:
 assert path in mounts, 'missing object filesystem'
 assert ('ro' in mounts[path]) == readonly, 'incorrect mount permission'
 os.stat(path)
"""


class KubernetesSidecarTransport(KubernetesMountTransport):
    name = "k8s-sidecar"

    def volume(self, grant):
        from opensandbox.models.sandboxes import OSSFS
        if os.getenv("STORAGE_TYPE", "").lower() != "s3":
            raise HTTPException(503, "Kubernetes spaces require external S3 storage")
        endpoint, bucket = os.getenv("S3_ENDPOINT", ""), os.getenv("S3_BUCKET", "")
        if not endpoint or not bucket:
            raise HTTPException(503, "External object mount configuration is incomplete")
        return self._volume(grant, sub_path=self._prepared_prefix(grant),
            ossfs=OSSFS(bucket=bucket, endpoint=endpoint, version="1.0",
                access_key_id="server-managed", access_key_secret="server-managed"))

    async def ensure(self, sandbox, actor, info, *, domain, api_key):
        """Check kernel mounts in the code container before every workspace use."""
        from opensandbox.models.execd import RunCommandOpts
        async with async_space_lock(actor):
            volumes, current = await asyncio.to_thread(prepare_volumes, actor)
            if current[MOUNT_REVISION_TAG] != (info.metadata or {}).get(MOUNT_REVISION_TAG):
                raise HTTPException(409, "文件权限已变更，挂载未恢复")
            expected = [(v.mount_path, v.read_only) for v in volumes]
            if not expected:
                return
            script = _CHECK_SCRIPT.replace("EXPECTED", repr(json.dumps(expected)))
            try:
                result = await asyncio.wait_for(sandbox.commands.run(
                    "python3 -c " + shlex.quote(script),
                    opts=RunCommandOpts(timeout=timedelta(seconds=8))), timeout=12)
                if result.exit_code != 0:
                    raise RuntimeError("Space filesystem is not mounted")
            except Exception as exc:
                raise HTTPException(503, "文件挂载未就绪，工作区已保留，请重试") from exc
