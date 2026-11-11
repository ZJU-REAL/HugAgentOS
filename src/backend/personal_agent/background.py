"""Background feature loops, separate from task leases and execution."""

import asyncio
import logging
import time
from sqlalchemy import select
from core.db.engine import SessionLocal
from personal_agent.models import PersonalAgent
from core.db.threads import run_in_session
from personal_agent.health import HealthEntry

log = logging.getLogger(__name__)
SCHEDULE_INTERVAL_SEC = 15
CHANNEL_INTERVAL_SEC = 3
SESSION_WATCH_INTERVAL_SEC = 10
MAINTENANCE_INTERVAL_SEC = 60
PROACTIVE_CLEANUP_SEC = 600
DREAMING_BEAT_SEC = 600   # the Dreaming loop sleeps until the next due time and beats in between
FILE_MEMORY_INTERVAL_SEC = 3
MEMORY_PRUNE_INTERVAL_SEC = 3600
TURN_BEAT_SEC = 10


def prune_memory_versions():
    """The write log keeps 30 days (memory.versions.KEEP_DAYS), each file's newest version always."""
    from personal_agent.memory import versions
    with SessionLocal() as db:
        while versions.prune(db):
            db.commit()


class BackgroundServices:
    def __init__(self, owner):
        self.owner = owner
        self.stopping = False
        self.tasks = []
        self.entries = []

    def start(self):
        from personal_agent.conversation.turn_worker import loop as turn_loop
        from personal_agent.workspace.file_memory import drain

        loops = [
            self.watch("turn", turn_loop(self.owner)),
            self.schedule_loop(),
            self.channel_loop(),
            self.prepare_schedules(),
            self.session_watch_loop(),
            self.maintenance_loop(),
            self.dreaming_loop(),
            self.every(FILE_MEMORY_INTERVAL_SEC, "file memory", lambda: asyncio.to_thread(drain)),
            self.every(MEMORY_PRUNE_INTERVAL_SEC, "memory versions", lambda: asyncio.to_thread(prune_memory_versions)),
        ]
        self.tasks = [asyncio.create_task(loop) for loop in loops]

    async def stop(self):
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        for entry in self.entries:
            await asyncio.to_thread(entry.unregister)
        self.entries.clear()

    async def register(self, name, seconds):
        """One HealthBoard row per loop; only that loop writes it."""
        entry = HealthEntry("loop." + name.replace(" ", "_"), self.owner, seconds)
        await asyncio.to_thread(entry.register)
        self.entries.append(entry)
        return entry

    async def every(self, seconds, name, cycle):
        health = await self.register(name, seconds)
        while not self.stopping:
            if health.beat_due:
                await asyncio.to_thread(health.beat)
            try:
                await cycle()
            except Exception as exc:
                log.warning("Personal %s cycle failed (%s)", name, type(exc).__name__)
                await asyncio.to_thread(health.error, f"{type(exc).__name__}: {exc}")
            else:
                if health.recovering:
                    await asyncio.to_thread(health.ok)
            await asyncio.sleep(seconds)

    async def watch(self, name, loop):
        """Beat while a loop that owns its own cycle runs; mark it down if it exits."""
        health = await self.register(name, TURN_BEAT_SEC)
        task = asyncio.ensure_future(loop)
        try:
            while not task.done():
                await asyncio.to_thread(health.beat)
                await asyncio.wait({task}, timeout=TURN_BEAT_SEC)
            if task.cancelled():
                return
            exc = task.exception()
            detail = f"{type(exc).__name__}: {exc}" if exc else "loop exited"
            log.warning("Personal %s loop stopped (%s)", name, detail)
            await asyncio.to_thread(health.error, detail, True)
        finally:
            task.cancel()

    async def dreaming_loop(self):
        """Not a fixed interval: runs when an agent is due and sleeps until the next one."""
        from personal_agent.memory.dreaming.pipeline import serve
        health = await self.register("dreaming", DREAMING_BEAT_SEC)

        async def beat():
            if health.beat_due: await asyncio.to_thread(health.beat)

        while not self.stopping:
            try:
                await serve(lambda: self.stopping, beat)
            except Exception as exc:
                log.warning("Personal dreaming cycle failed (%s)", type(exc).__name__)
                await asyncio.to_thread(health.error, f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(60)

    async def schedule_loop(self):
        from personal_agent.scheduling.schedules import fire_due

        def fire():
            with SessionLocal() as db:
                return fire_due(db)

        await self.every(SCHEDULE_INTERVAL_SEC, "schedule", lambda: asyncio.to_thread(fire))

    async def prepare_schedules(self):
        """Once, at start: fill next_due_at where it is missing and schedule the agents that existed before
        proactive checks did. Not a loop: afterwards new agents are scheduled when they are created."""
        from personal_agent.scheduling import heartbeat, schedules

        def prepare():
            with SessionLocal() as db:
                schedules.backfill(db)
            with SessionLocal() as db:
                heartbeat.seed_all(db)

        try:
            await asyncio.to_thread(prepare)
        except Exception as exc:
            log.warning("Personal schedule preparation failed (%s)", type(exc).__name__)

    async def channel_loop(self):
        from personal_agent.channels.channel_bridge import deliver_pending

        await self.every(CHANNEL_INTERVAL_SEC, "channel", deliver_pending)

    async def session_watch_loop(self):
        """Report the end of HugAgentOS chat runs that tasks started."""
        from personal_agent.sessions.session_watch import report_finished

        await self.every(
            SESSION_WATCH_INTERVAL_SEC, "session watch", lambda: asyncio.to_thread(report_finished)
        )

    async def maintenance_loop(self):
        last_cleanup = 0.0

        def proactive():
            """Void the approvals a proactive task asked for that nobody answered for a day; now and then remove the rows of quiet checks and old drafts."""
            nonlocal last_cleanup
            from personal_agent.scheduling import heartbeat, proactive_tasks
            with SessionLocal() as db:
                proactive_tasks.expire(db)
            if time.monotonic() - last_cleanup > PROACTIVE_CLEANUP_SEC:
                with SessionLocal() as db:
                    heartbeat.cleanup(db)
                # Finished computer commands and session reads carry screenshots and chat histories.
                from personal_agent.computer.devices import prune_commands
                from personal_agent.sessions.session_queue import prune
                with SessionLocal() as db:
                    prune_commands(db)
                with SessionLocal() as db:
                    prune(db)
                last_cleanup = time.monotonic()

        async def maintain():
            try:
                await asyncio.to_thread(proactive)
            except Exception as exc:
                log.warning("Personal proactive maintenance failed (%s)", type(exc).__name__)
            await self.maintain_computers()

        await self.every(MAINTENANCE_INTERVAL_SEC, "computer maintenance", maintain)

    async def maintain_computers(self):
        def _computer_bindings(db):
            bindings = db.execute(
                select(PersonalAgent.id, PersonalAgent.sandbox_session_id, PersonalAgent.user_id)
            ).all()
            return bindings
        bindings = await run_in_session(_computer_bindings)
        if not bindings:
            return
        from personal_agent.computer.sandbox import ProvisionInProgress, get_personal_sandbox_provider

        for ident, box, uid in bindings:
            try:
                async with asyncio.timeout(45):
                    provider = get_personal_sandbox_provider()
                    await provider.ensure_user_workspace(box, uid)
                    await provider.touch_session(box)
            except ProvisionInProgress:
                # Another worker is creating it and records the outcome.
                continue
            except Exception as exc:
                log.warning("Personal computer %s maintenance failed: %s: %s", ident, type(exc).__name__, exc)
                def _mark_degraded(db):
                    row = db.get(PersonalAgent, ident)
                    if row:
                        row.computer_status = "degraded"
                    db.commit()
                await run_in_session(_mark_degraded)
