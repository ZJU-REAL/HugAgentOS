"""Bounded streaming of sandbox files into host storage."""

from __future__ import annotations

from collections.abc import AsyncIterable
from pathlib import Path

from core.sandbox.errors import SandboxFileTooLargeError


async def stream_to_file(chunks: AsyncIterable[bytes], destination: Path, *, max_bytes: int) -> int:
    """Write an async byte stream to ``destination`` with a hard byte limit.

    The partial destination is removed on any failure, including a stream that
    grows beyond ``max_bytes`` after its initial metadata check.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with destination.open("wb") as output:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise SandboxFileTooLargeError(actual_size=total, max_size=max_bytes)
                output.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return total
