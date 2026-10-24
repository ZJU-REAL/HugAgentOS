"""Bounded pool of chat-scoped STDIO sessions shared by that chat's concurrent runs.

A session is keyed by the trusted executing user, chat and capability scope plus the
server configuration. Runs hold counted leases; the call gate inside the session
orders writes, so a run never waits for another run to finish. Idle sessions
expire, and are reclaimed oldest-first when capacity is needed.
"""
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass

IDLE_SECONDS = 60.0
MAX_SESSIONS = 32
MAX_USER_SESSIONS = 4
_entries = {}


@dataclass
class Entry:
    key: tuple
    loop: object
    session: object = None
    opening: asyncio.Task | None = None
    expiry: asyncio.Task | None = None
    holders: int = 0
    idle_at: float = 0.0
    retired: bool = False

    @property
    def idle(self):
        return self.holders == 0 and self.opening is None

    def detach(self):
        """Stop handing this session out; its holders keep it until they release."""
        if _entries.get(self.key) is self:
            del _entries[self.key]
        self.retired = True
        if self.expiry is not None:
            self.expiry.cancel()
            self.expiry = None


class Lease:
    def __init__(self, entry):
        self.entry, self.session = entry, entry.session
        self._released = False

    async def release(self, *, abort=False):
        if self._released:
            return
        self._released = True
        entry = self.entry
        entry.holders -= 1
        if abort or self.session.stopped:
            entry.detach()
        if entry.holders:
            return
        if entry.retired:
            await self.session.close()
        else:
            _park(entry)


async def _expire(entry):
    await asyncio.sleep(IDLE_SECONDS)
    if entry.idle and _entries.get(entry.key) is entry:
        entry.detach()
        await entry.session.close()


async def _reclaim(user):
    """Make room for one more session, closing idle sessions oldest-first."""
    while True:
        user_entries = [e for k, e in _entries.items() if k[0] == user]
        if len(user_entries) >= MAX_USER_SESSIONS:
            candidates = user_entries
        elif len(_entries) >= MAX_SESSIONS:
            candidates = list(_entries.values())
        else:
            return
        idle = [e for e in candidates if e.idle]
        if not idle:
            raise RuntimeError("MCP STDIO session capacity reached")
        victim = min(idle, key=lambda e: e.idle_at)
        victim.detach()
        await victim.session.close()


def _key(scope, name, cfg):
    from mcp.client.stdio import get_default_environment
    cfg = {**cfg, "env": get_default_environment() | (cfg.get("env") or {})}
    cfg.pop("oauth_provider", None)
    fingerprint = hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return (*scope, name, fingerprint)


def _park(entry):
    """An unheld session waits out its idle period, or closes now if it was retired."""
    if entry.retired:
        return asyncio.create_task(entry.session.close())
    entry.idle_at = time.monotonic()
    entry.expiry = asyncio.create_task(_expire(entry))


async def _open(entry, name, cfg):
    from core.llm.mcp.session import Session
    try:
        session = await Session.open(name, cfg)
    except BaseException:
        if entry.opening is asyncio.current_task():
            entry.opening = None
            entry.detach()
        raise
    entry.opening = None
    entry.session = session
    if entry.retired:
        await session.close()
        raise RuntimeError("MCP chat session was revoked")
    if entry.holders == 0:
        _park(entry)
    return session


async def acquire(scope, name, cfg):
    # Scope contains trusted executing user, chat and capability scope, not remote metadata.
    key = _key(scope, name, cfg)
    for old in [e for k, e in _entries.items() if k[:-1] == key[:-1] and k != key]:
        old.detach()
        if old.idle and old.session is not None:
            await old.session.close()
    entry = _entries.get(key)
    if entry is not None and (entry.retired or entry.session is not None and entry.session.stopped):
        entry.detach()
        entry = None
    if entry is None:
        await _reclaim(scope[0])
        entry = _entries.setdefault(key, Entry(key, asyncio.get_running_loop()))
    if entry.expiry is not None:
        entry.expiry.cancel()
        entry.expiry = None
    entry.holders += 1
    try:
        if entry.session is None:
            if entry.opening is None:
                entry.opening = asyncio.create_task(_open(entry, name, cfg), name=f"mcp-open:{name}")
            await asyncio.shield(entry.opening)
        if entry.retired or entry.session.stopped:
            raise RuntimeError("MCP chat session was revoked")
        return Lease(entry)
    except BaseException:
        entry.holders -= 1
        if entry.holders == 0 and entry.session is not None and entry.expiry is None:
            _park(entry)
        raise


async def close_chat(chat_id=None):
    """Revoke every session of a chat (or all); holders' next calls fail as closed."""
    for entry in [e for k, e in list(_entries.items()) if chat_id is None or k[1] == chat_id]:
        entry.detach()
        if entry.opening is not None:
            opening, entry.opening = entry.opening, None
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
        if entry.session is not None:
            entry.session.failed = True
            await entry.session.close()


def invalidate_chat(chat_id):
    """Deletion can run on a sync API worker; dispatch teardown to the owning loop."""
    entries = [entry for key, entry in list(_entries.items()) if key[1] == chat_id]
    for entry in entries:
        # Plain flag writes take effect at once; dict and task work stays on the owning loop.
        entry.retired = True
        if entry.session is not None:
            entry.session.failed = True
    for loop in {entry.loop for entry in entries}:
        loop.call_soon_threadsafe(lambda: asyncio.create_task(close_chat(chat_id)))
