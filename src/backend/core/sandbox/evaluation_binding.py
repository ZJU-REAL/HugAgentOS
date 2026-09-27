"""Strict, shared evaluation leases; absent state never authorizes a new sandbox."""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field

from core.infra.ephemeral import get_ephemeral_state
from .errors import SandboxError

PREFIX = "jx:sandbox:evaluation:"
PHASES = {"ready", "active", "freezing", "frozen", "closed"}


@dataclass(frozen=True)
class Binding:
    lease_id: str
    session_id: str
    chat_id: str
    owner_user_id: str
    sandbox_id: str
    phase: str
    expires_at: float
    operations: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)
    uncertain: bool = False
    protected_processes: dict[str, str] = field(default_factory=dict)
    destroyed: bool = False


def _key(session_id):
    if not isinstance(session_id, str) or not session_id.startswith("eval_"):
        raise SandboxError("Evaluation session required")
    try:
        if uuid.UUID(session_id[5:]).hex != session_id[5:]:
            raise ValueError()
    except ValueError:
        raise SandboxError("Invalid evaluation session") from None
    return PREFIX + session_id


def _decode(raw, session_id, *, allow_expired=False):
    try:
        item = Binding(**json.loads(raw))
        if (item.session_id != session_id or item.chat_id != session_id
                or item.lease_id != session_id[5:] or item.phase not in PHASES
                or not item.owner_user_id or not item.sandbox_id
                or not isinstance(item.operations, dict) or not isinstance(item.commands, dict)
                or not math.isfinite(item.expires_at)):
            raise ValueError()
    except (TypeError, ValueError, KeyError):
        raise SandboxError("Evaluation binding missing or invalid") from None
    if not allow_expired and item.expires_at <= time.time():
        raise SandboxError("Evaluation binding expired")
    return item


async def get(session_id: str, *, allow_expired=False) -> Binding:
    raw = await get_ephemeral_state().get(_key(session_id))
    return _decode(raw, session_id, allow_expired=allow_expired)


async def assert_owner(session_id: str, owner_user_id: str, *, allow_expired=False) -> Binding:
    item = await get(session_id, allow_expired=allow_expired)
    if not owner_user_id or item.owner_user_id != owner_user_id:
        raise SandboxError("Evaluation binding owner mismatch")
    return item


def _ttl(item):
    # Retain expired and closed tombstones beyond the live lease.
    return max(3600, math.ceil(item.expires_at - time.time()) + 3600)


async def create(owner_user_id: str, sandbox_id: str, ttl: int, lease_id=None,
                 *, protected_processes=None) -> Binding:
    if not owner_user_id or not sandbox_id or not 1 <= ttl <= 86400:
        raise SandboxError("Invalid evaluation lease parameters")
    identity = uuid.uuid4().hex if lease_id is None else uuid.UUID(str(lease_id)).hex
    session_id = "eval_" + identity
    item = Binding(identity, session_id, session_id, owner_user_id, sandbox_id,
                   "ready", time.time() + ttl, protected_processes=dict(protected_processes or {}))
    if not await get_ephemeral_state().hold(_key(session_id), json.dumps(asdict(item)), ttl=_ttl(item)):
        raise SandboxError("Evaluation lease already exists")
    return item


async def _change(session_id, owner, change, *, allow_expired=False):
    state, key = get_ephemeral_state(), _key(session_id)
    # CAS includes phase and operation admission in one record: freezing cannot
    # race a separate counter increment, and crashed workers fail closed.
    for _ in range(128):
        raw = await state.get(key)
        item = _decode(raw, session_id, allow_expired=allow_expired)
        if owner != item.owner_user_id or not owner:
            raise SandboxError("Evaluation binding owner mismatch")
        values = asdict(item)
        change(values)
        updated = Binding(**values)
        if await state.compare_exchange(key, raw, json.dumps(values), ttl=_ttl(updated)):
            return updated
        await asyncio.sleep(0)
    raise SandboxError("Evaluation binding contention; retry explicitly")


async def activate(session_id, owner):
    def change(value):
        if value["phase"] not in {"ready", "active"}:
            raise SandboxError("Evaluation binding is not writable")
        value["phase"] = "active"
    return await _change(session_id, owner, change)


@asynccontextmanager
async def operation(session_id, owner, *, resource=None):
    token = uuid.uuid4().hex
    def enter(value):
        if value["phase"] not in {"ready", "active"}:
            raise SandboxError("Evaluation agent operations are frozen or closed")
        if resource and resource in value["operations"].values():
            raise SandboxError("Evaluation resource operation already active")
        value["operations"][token] = resource or token
        value["phase"] = "active"
    item = await _change(session_id, owner, enter)
    try:
        yield item
    finally:
        def leave(value):
            value["operations"].pop(token, None)
        cleanup = asyncio.create_task(_change(session_id, owner, leave, allow_expired=True))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


async def record_command(session_id, owner, handle, command):
    def change(value):
        # Existing admitted launches may finish registration while freezing.
        if value["phase"] not in {"active", "freezing"} or not value["operations"]:
            raise SandboxError("Command registration has no active operation")
        value["commands"][handle] = command
    return await _change(session_id, owner, change)


async def uncertain(session_id, owner):
    def change(value):
        value["uncertain"] = True
        if value["phase"] != "closed":
            value["phase"] = "freezing"
    return await _change(session_id, owner, change, allow_expired=True)


async def begin_freeze(session_id, owner):
    def change(value):
        if value["phase"] == "closed":
            raise SandboxError("Evaluation binding is closed")
        if value["phase"] != "frozen":
            value["phase"] = "freezing"
    return await _change(session_id, owner, change)


async def wait_idle(session_id, owner, *, timeout=65):
    deadline = time.monotonic() + timeout
    while True:
        item = await assert_owner(session_id, owner)
        if not item.operations:
            return item
        if time.monotonic() >= deadline:
            raise SandboxError("Evaluation operations did not drain; lease remains frozen to agents")
        await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))


async def finish_freeze(session_id, owner):
    def change(value):
        if value["phase"] not in {"freezing", "frozen"} or value["operations"] or value["uncertain"]:
            raise SandboxError("Evaluation freeze cannot be confirmed")
        value["phase"] = "frozen"
    return await _change(session_id, owner, change)


async def close(session_id, owner):
    def change(value):
        value["phase"] = "closed"
    return await _change(session_id, owner, change, allow_expired=True)


async def mark_destroyed(session_id, owner):
    def change(value):
        value["phase"] = "closed"
        value["destroyed"] = True
    return await _change(session_id, owner, change, allow_expired=True)
