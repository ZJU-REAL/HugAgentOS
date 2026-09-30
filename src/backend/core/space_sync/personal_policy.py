"""Personal-space approval routing and byte-based backpressure."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from core.myspace import mirror

logger = logging.getLogger(__name__)
_INFLIGHT_BUDGET_BYTES = 256 * 1024 * 1024
_MIN_COST_BYTES = 32 * 1024 * 1024
_MEMORY_FACTOR = 3


def find_chat(user_id: str) -> Optional[str]:
    "确认条弹到哪个会话里去；没处可弹则 ``None``。"
    try:
        from orchestration import chat_run_executor as cre

        if not cre.has_local_runs():  # 便宜的前置判断，省掉一次 DB 查询
            return None
        for run in cre.list_active_runs_for_user(user_id):
            if cre.is_local_run(str(run.run_id)):
                return str(run.chat_id)
    except Exception as exc:  # noqa: BLE001 — 查不到就按"问不到人"处理
        logger.warning("[myspace-registry] 查活跃会话失败 user=%s: %s", user_id, exc)
    return None


class Budget:
    "同时在办的登记占多少内存，按字节限流。"

    def __init__(self, total: int) -> None:
        self._total = total
        self._used = 0
        self._cv = asyncio.Condition()

    def cost(self, size: Optional[int]) -> int:
        want = max(int(size or 0) * _MEMORY_FACTOR, _MIN_COST_BYTES)
        return min(want, self._total)

    @asynccontextmanager
    async def reserve(self, size: Optional[int]) -> AsyncIterator[None]:
        cost = self.cost(size)
        async with self._cv:
            while self._used + cost > self._total and self._used > 0:
                await self._cv.wait()
            self._used += cost
        try:
            yield
        finally:
            async with self._cv:
                self._used -= cost
                self._cv.notify_all()


class Ask:
    "确认条问谁 —— 查一次、缓存一次。"

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id
        self._chat: Optional[str] = None
        self._asked = False

    async def get(self) -> Optional[str]:
        if not self._asked:
            self._chat = await asyncio.to_thread(find_chat, self._user_id)
            self._asked = True
        return self._chat


def _split(raw: str, root: Path) -> Optional[tuple[str, str]]:
    "把文件系统路径拆成 ``(user_id, 相对用户根目录的路径)``；不该管的返回 ``None``。"
    try:
        rel = Path(raw).relative_to(root)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None  # 用户目录本身，不是用户的文件
    if any(p in mirror.SKIP_DIR_NAMES for p in parts[1:]):
        return None
    return parts[0], "/".join(parts[1:])
