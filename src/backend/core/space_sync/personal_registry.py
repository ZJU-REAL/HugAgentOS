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


async def flush_user(
    user_id: str, *, metadata_only: bool = False, paths: tuple[str, ...] = (), strict: bool = False
) -> None:
    "催一下这个用户待登记的改动 —— 读「我的空间」之前调，看到的就是当下状态。"
    if _registry is None or not user_id:
        return
    from core.myspace.projection import pull_myspace_updates
    from fastapi import HTTPException

    try:
        # Explicit paths close the gap between a file tool returning and watchdog
        # delivering its event. They still use the ordinary authorization/conflict
        # policy, queue, memory budget and registration transaction.
        from pathlib import PurePosixPath
        from time import time

        from .events import Change

        for path in paths:
            if (
                not path
                or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or "\\" in path
            ):
                raise ValueError("Invalid personal source path")
            _registry._receive(Change("modified", user_id + "/" + path, observed=time()))
        try:
            await _registry.flush(
                user_id, metadata_only=metadata_only, strict=strict or bool(paths), paths=paths
            )
        except (RuntimeError, TimeoutError):
            if strict or paths:
                raise
            logger.warning(
                "[myspace-registry] Pending writes retained; serving committed data user=%s",
                user_id,
            )
        if metadata_only:
            return
        report = await asyncio.to_thread(pull_myspace_updates, user_id=user_id, paths=paths)
        if report.failed or report.conflicted:
            if strict or paths:
                raise RuntimeError("部分空间文件尚未同步")
            logger.warning(
                "[myspace-registry] Partial projection retained user=%s failed=%d conflicted=%d",
                user_id,
                report.failed,
                report.conflicted,
            )
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
