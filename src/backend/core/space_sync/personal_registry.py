"""Application lifecycle and consistent-read barriers for the personal-space listener."""

import asyncio
import logging
from typing import Optional

from .personal import MySpaceRegistry

logger = logging.getLogger(__name__)

_registry: Optional[MySpaceRegistry] = None


async def start_registry() -> None:
    global _registry
    if _registry is not None:
        return
    candidate = MySpaceRegistry()
    try:
        await candidate.start()
    except Exception:
        await candidate.stop()
        raise
    _registry = candidate


def get_registry() -> Optional[MySpaceRegistry]:
    "当前进程的登记器；没起来时返回 ``None``（后台 worker 健康报告读它）。"
    return _registry


async def flush_user(user_id: str, *, metadata_only: bool = False) -> None:
    "催一下这个用户待登记的改动 —— 读「我的空间」之前调，看到的就是当下状态。"
    if _registry is None or not user_id:
        return
    from core.myspace.projection import pull_myspace_updates
    from fastapi import HTTPException

    try:
        if metadata_only:
            await _registry.flush(user_id, metadata_only=True)
            return
        await _registry.flush(user_id)
        report = await asyncio.to_thread(pull_myspace_updates, user_id=user_id)
        if report.failed or report.conflicted:
            raise RuntimeError("部分空间文件尚未同步")
    except Exception as exc:
        logger.warning(
            "[myspace-registry] 同步屏障未完成 user=%s error=%s", user_id, type(exc).__name__
        )
        raise HTTPException(409, "空间文件同步未完成，本地修改已保留，请稍后重试") from exc


async def stop_registry() -> None:
    global _registry
    if _registry is None:
        return
    await _registry.stop()
    _registry = None
