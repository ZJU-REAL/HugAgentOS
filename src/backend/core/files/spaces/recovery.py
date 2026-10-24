"""Restore authorized mounts in the existing sandbox after controller restart."""
import asyncio
import os

import httpx
from fastapi import HTTPException

from core.files.spaces.leases import async_space_lock
from core.files.spaces.mounts import MOUNT_REVISION_TAG, prepare_volumes

EPOCH_TAG = "hugagent-mount-epoch"

async def ensure_mounts(sandbox, actor, info, *, domain, api_key):
    if os.getenv("STORAGE_TYPE", "local").lower() == "local":
        return
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
