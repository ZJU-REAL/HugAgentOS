"""Local-process session leases. Secret-bearing tool configs never enter durable storage.

This module is the provider behind ``core.harness.registry.client_sessions``.
"""
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from uuid import uuid4

TTL_SECONDS = 180
MAX_LEASES = 128
_lock = RLock()
_entries = {}

@dataclass
class Lease:
    id: str
    user: str
    chat: str
    cwd: str
    mode: str
    servers: dict
    expires: float
    run_id: str | None = None
    revoked: bool = False
    run_mode: str | None = None

def create(user, chat, cwd, mode, servers):
    with _lock:
        if chat in _entries or len(_entries) >= MAX_LEASES:
            raise ValueError("Client session already leased or capacity reached")
        lease = Lease(uuid4().hex, user, chat, cwd, mode, deepcopy(servers), monotonic() + TTL_SECONDS)
        _entries[chat] = lease
        return lease.id

def require(user, chat, lease_id):
    with _lock:
        lease = _entries.get(chat)
        if lease is None or lease.user != user or lease.id != lease_id or lease.expires <= monotonic() or lease.revoked:
            raise ValueError("Client session lease is unavailable")
        return deepcopy(lease)

def update(user, chat, lease_id, mode):
    with _lock:
        require(user, chat, lease_id)
        lease = _entries[chat]
        lease.expires = monotonic() + TTL_SECONDS
        if mode is not None:
            lease.mode = mode

def active(user, chat):
    from core.config.local_mode import local_mode_enabled
    if not local_mode_enabled():
        return None
    with _lock:
        lease = _entries.get(chat)
        return deepcopy(lease) if lease and lease.user == user else None

def approval_mode(user, chat=None, run_id=None):
    """Preset of the client session owning this chat (or run), else None.

    A revoked or expired lease raises instead of answering None: work it once
    owned must never fall back to the account's preset.
    """
    from core.config.local_mode import local_mode_enabled
    if not local_mode_enabled() or not (chat or run_id):
        return None
    with _lock:
        if chat:
            lease = _entries.get(chat)
        else:
            lease = next((entry for entry in _entries.values() if entry.run_id == run_id), None)
        if lease is None or lease.user != user:
            return None
        current = require(user, lease.chat, lease.id)
    return current.run_mode or current.mode

def servers_for(user, chat, run_id):
    lease = active(user, chat)
    if lease is None or lease.run_id != run_id:
        return {}
    require(user, chat, lease.id)
    return {f"client_{lease.id}_{name}": {**config, "client_session_lease": (user, chat, lease.id)}
            for name, config in lease.servers.items()}


def admit(user, chat, lease_id):
    lease = active(user, chat)
    if lease_id is None and lease is None:
        return None
    if lease_id is None or lease is None:
        raise ValueError("Client session lease required")
    return require(user, chat, lease_id)


def bind_run(user, chat, lease_id, run_id, mode):
    with _lock:
        require(user, chat, lease_id)
        _entries[chat].run_id = run_id
        _entries[chat].run_mode = mode


async def revoke(user, chat, lease_id):
    with _lock:
        lease = _entries.get(chat)
        if lease is None or lease.user != user or lease.id != lease_id or lease.revoked:
            return
        lease.revoked = True
    cleaned = False
    try:
        if lease.run_id:
            from core.runs.runtime import cancel_run, ChatRunNotFound
            try:
                await cancel_run(lease.run_id, user_id=user)
            except ChatRunNotFound:
                pass  # Admission rechecks immediately after starting the run.
        from core.llm.mcp.chat_sessions import close_chat
        await close_chat(chat)
        cleaned = True
    finally:
        with _lock:
            if _entries.get(chat) is lease:
                lease.servers.clear()
                if cleaned:
                    del _entries[chat]
                # Failed cleanup retains a revoked tombstone: never fall back to
                # the account's permissions while a formerly leased run may live.


async def watch(user, chat, lease_id):
    import asyncio
    try:
        while True:
            with _lock:
                lease = _entries.get(chat)
                if lease is None or lease.id != lease_id:
                    return
                remaining = lease.expires - monotonic()
            if remaining <= 0:
                await revoke(user, chat, lease_id)
                return
            await asyncio.sleep(min(remaining, 5))
    finally:
        await revoke(user, chat, lease_id)
