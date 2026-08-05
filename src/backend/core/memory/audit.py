"""Memory audit side-channel — community-edition no-op stub.

Memory auditing (hash-only storage, trace chains) is a commercial-edition
compliance capability; the community edition keeps the same module surface but
records nothing, so callers (the L1 profile writer, the extraction pipeline)
need zero changes.

The stubs must stay *call-compatible* with the EE implementation, whose real
signatures are ``record(ctx, action, layer, *, memory_id=None, content=None,
reason=None)`` etc. — the first three are positional. Narrowing the stub
signature makes positional calls raise TypeError, and that raise lands *after*
the business transaction has committed, turning a successful write into a
reported failure (bitten in the 0.2.15 desktop local build: the L1 preference
was persisted, yet the UI never showed the write card). Hence ``*args`` /
``**kwargs`` catch-alls.
"""

from __future__ import annotations

from typing import Any


async def record(*args: Any, **kwargs: Any) -> None:
    return None


async def record_batch(*args: Any, **kwargs: Any) -> None:
    return None


def record_sync(*args: Any, **kwargs: Any) -> None:
    return None


__all__ = ["record", "record_batch", "record_sync"]
