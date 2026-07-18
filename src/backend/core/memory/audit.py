"""记忆审计旁路 —— 社区版 no-op stub。

记忆审计（存 hash 不存原文、链路追溯）属商业版合规能力；社区版保持
同名接口但不落任何审计数据，调用方（L1 画像 / 抽取写入器）零改动。
"""

from __future__ import annotations

from typing import Any, Iterable


async def record(ctx: Any = None, **kwargs: Any) -> None:
    return None


async def record_batch(ctx: Any = None, items: Iterable[Any] | None = None, **kwargs: Any) -> None:
    return None


def record_sync(ctx: Any = None, **kwargs: Any) -> None:
    return None


__all__ = ["record", "record_batch", "record_sync"]
