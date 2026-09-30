"""Retryable per-event leases; a failed worker never marks an event complete."""
import hashlib
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

_claims = ContextVar("myspace_claims", default=None)


@asynccontextmanager
async def claim_batch():
    held = []
    context = _claims.set(held)
    complete = False
    try:
        yield
        complete = True
    finally:
        from core.infra.ephemeral import get_ephemeral_state
        state = get_ephemeral_state()
        try:
            for key, token in held:
                await state.compare_exchange(
                    key, token, "done" if complete else "retry", ttl=300 if complete else 1
                )
        finally:
            _claims.reset(context)


async def claim(user_id, rel, stamp):
    from core.infra.ephemeral import get_ephemeral_state
    state = get_ephemeral_state()
    digest = hashlib.sha256(f"{user_id}/{rel}".encode()).hexdigest()
    key = f"jx:myspace:registry-v2:{user_id}:{digest}:{stamp}"
    token = uuid.uuid4().hex
    acquired = await state.hold(key, token, ttl=300)
    if not acquired:
        value = await state.get(key)
        if value == "done":
            return False
        if value == "retry":
            acquired = await state.compare_exchange(key, "retry", token, ttl=300)
        if not acquired:
            raise RuntimeError("MySpace registration is still owned by another worker")
    held = _claims.get()
    if held is not None:
        held.append((key, token))
    return True
