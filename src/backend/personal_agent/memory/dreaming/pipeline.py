"""One Dreaming run: claim → collect → snapshot → analyze each source → archive/delete by use → organize → finish.

serve() runs tick() whenever an agent is due and sleeps until the next one. Database work runs in
threads with its own session; model calls run on the event loop between them. Any
exception after the snapshot rolls memory/ back to it and leaves the receipts undreamed.
"""
import asyncio
import time
from datetime import datetime

from core.db.engine import SessionLocal
from personal_agent.memory import seed, store
from personal_agent.memory.dreaming import agents, collect, finish, loop, runs
from personal_agent.scheduling.schedules import user_zone, zone


def _today(sessions, ident, now):
    from personal_agent.models import PersonalAgent
    with sessions() as db:
        agent = db.get(PersonalAgent, ident)
        return datetime.fromtimestamp(now, zone(user_zone(agent.config if agent else {}))).date().isoformat()


async def dream(sessions, ident, run_id, owner, attempt, now, make_model):
    snapshot_id, results = None, []
    try:
        # An attempt that died without abort() (a crash or a kill) left memory/ half
        # written: put back the run's snapshot before anything is collected or snapshotted.
        await asyncio.to_thread(collect.recover, sessions, ident, run_id, owner)
        batch = await asyncio.to_thread(collect.prepare, sessions, ident, run_id, owner)
        if not batch.sources:
            await asyncio.to_thread(finish.empty, sessions, ident, run_id, owner, batch, now)
            return
        today = await asyncio.to_thread(_today, sessions, ident, now)
        snapshot_id = await asyncio.to_thread(collect.snapshot, sessions, ident, run_id, owner)
        before = await asyncio.to_thread(store.wiki, ident)
        model = await asyncio.to_thread(make_model, batch.provider)   # resolving the provider reads the database
        for source in batch.sources:
            results.append(await agents.analyze(sessions, ident, run_id, owner, model, source, today))
        if not any(item["ok"] for item in results):
            raise RuntimeError("没有一个来源分析成功：" + str(results[0].get("error")))
        life = await asyncio.to_thread(agents.lifecycle, sessions, ident, run_id, owner, today)
        task = await asyncio.to_thread(agents.brief, ident, batch.source_bytes, today, sum(item["ok"] for item in results), life)
        organized = {**await agents.organize(sessions, ident, run_id, owner, model, task), "lifecycle": life}
        await asyncio.to_thread(finish.finish, sessions, ident, run_id, owner, batch, results, organized, before, now)
    except BaseException as exc:
        # CancelledError (shutdown) too: roll back now rather than leave it to recover().
        failed = [item["receipt"] for item in results if not item["ok"]]
        await asyncio.shield(asyncio.to_thread(finish.abort, sessions, ident, run_id, owner, snapshot_id, exc, now, failed))
        if not isinstance(exc, Exception): raise


def _seeded(ident):
    from personal_agent.workspace.files import WorkspaceUnavailable
    try:
        seed.seed(ident)
    except (BlockingIOError, WorkspaceUnavailable, OSError):
        pass   # the next tick tries again; claim still runs on whatever is there


async def tick(sessions=None, now=None, make_model=None):
    """Run every due agent's Dreaming once; returns when any agent is next due (or None).

    The loop (serve) sleeps until then. make_model(provider_id) replaces the model in tests."""
    sessions = sessions or SessionLocal
    make_model = make_model or loop.build_model
    due, _later = await asyncio.to_thread(runs.plan, sessions, time.time() if now is None else now)
    for ident in due:
        moment = time.time() if now is None else now
        # A workspace no turn has opened since an upgrade is still in the old layout: seed and
        # migrate it first, or the run would snapshot and lint a wiki that is not there yet.
        await asyncio.to_thread(_seeded, ident)
        claimed = await asyncio.to_thread(runs.claim, sessions, ident, moment)
        if not claimed: continue
        run_id, owner, attempt = claimed
        await dream(sessions, ident, run_id, owner, attempt, moment, make_model)
    # Recomputed after the runs: one that finished may have left a continuation, or failed into a retry.
    _due, later = await asyncio.to_thread(runs.plan, sessions, time.time() if now is None else now)
    return time.time() if _due else later


MAX_IDLE = 3600   # a missed wake-up (a lost notification, an agent created since) waits at most this long


async def serve(stopping, beat=None, sessions=None):
    """The Dreaming loop: run what is due, then sleep until the next due time or a wake-up.

    No fixed polling: the nightly slot, a retry time and a lease expiry are known in advance;
    "run now" and a settings change wake it at once (wake.notify)."""
    from personal_agent.memory.dreaming import wake
    while not stopping():
        if beat is not None: await beat()
        next_at = await tick(sessions=sessions)
        delay = MAX_IDLE if next_at is None else min(MAX_IDLE, max(0.0, next_at - time.time()))
        if delay > 0: await wake.wait(delay, beat=beat, stopping=stopping)
