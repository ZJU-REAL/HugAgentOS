"""Docker deployments with object storage mount through the controller's FUSE worker."""
import asyncio
import os

import httpx
from fastapi import HTTPException

from core.files.spaces.leases import async_space_lock
from core.files.spaces.mounts import MOUNT_REVISION_TAG, prepare_volumes
from core.files.spaces.transport.base import SpaceTransport
from core.files.storage.entries import validate_key

EPOCH_TAG = "hugagent-mount-epoch"


class ObjectFsTransport(SpaceTransport):
    name = "object-fs"

    def volume(self, grant):
        from opensandbox.models.sandboxes import OSSFS

        backend = os.getenv("STORAGE_TYPE", "local").lower()
        if backend not in ("s3", "oss", "obs"):
            raise HTTPException(503, "Unsupported storage provider")
        prefix = backend.upper()
        endpoint = os.getenv(prefix + "_ENDPOINT", "")
        bucket = os.getenv(prefix + "_BUCKET", "")
        access = os.getenv(prefix + ("_ACCESS_KEY_ID" if backend == "oss" else "_ACCESS_KEY"), "")
        secret = os.getenv(prefix + ("_ACCESS_KEY_SECRET" if backend == "oss" else "_SECRET_KEY"), "")
        if not all((endpoint, bucket, access, secret)):
            raise HTTPException(503, "Object mount configuration is incomplete")
        key_prefix = os.getenv("OSS_KEY_PREFIX", "").strip("/") if backend == "oss" else ""
        path = "/".join(part for part in (key_prefix, grant.prefix.rstrip("/")) if part)
        validate_key(path)
        options = ["hugagent_driver=s3fs", "allow_other", "uid=1000", "gid=1000",
            "umask=0022", "stat_cache_expire=0", "attr_timeout=0", "entry_timeout=0",
            "negative_timeout=0"]
        if backend == "oss":
            options.append("sigv2")
        else:
            options.extend(["sigv4", "endpoint=" + os.getenv(prefix + "_REGION", "us-east-1")])
            if backend == "s3":
                options.append("use_path_request_style")
        if not grant.writable:
            options.append("ro")
        return self._volume(grant, sub_path=path, ossfs=OSSFS(bucket=bucket, endpoint=endpoint,
            version="1.0", access_key_id=access, access_key_secret=secret, options=options))

    async def ensure(self, sandbox, actor, info, *, domain, api_key):
        metadata = info.metadata or {}
        epoch = metadata.get(EPOCH_TAG)
        if not epoch:
            raise HTTPException(503, "挂载控制器尚未就绪，工作区已保留")
        # Controller identity does not imply sandbox or FUSE-owner health.
        # Every use checks the live mounts, including after a sandbox restart.
        async with async_space_lock(actor):
            # Re-resolve while holding the same lock as membership changes. Never
            # recover volumes from a historical permission snapshot.
            volumes, current = await asyncio.to_thread(prepare_volumes, actor)
            if current[MOUNT_REVISION_TAG] != metadata.get(MOUNT_REVISION_TAG):
                raise HTTPException(409, "文件权限已变更，挂载未恢复")
            base = domain if "://" in domain else "http://" + domain
            headers = {"OPEN-SANDBOX-API-KEY": api_key} if api_key else {}
            async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
                response = await client.post(
                    base.rstrip("/") + "/v1/sandboxes/" + str(sandbox.id) + "/space-mounts/ensure",
                    headers=headers, json={"actor": actor, "revision": current[MOUNT_REVISION_TAG],
                        "volumes": [v.model_dump(mode="json", by_alias=True) for v in volumes]},
                )
            if response.status_code == 409:
                raise HTTPException(409, "文件挂载权限或配置已变更，沙盒和工作区已保留，请联系管理员")
            if response.status_code != 200:
                raise HTTPException(503, "文件挂载恢复失败，沙盒和工作区已保留，请重试")
            if response.json().get("epoch") != epoch:
                raise HTTPException(503, "挂载控制器正在重启，请重试")
