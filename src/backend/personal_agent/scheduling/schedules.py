"""Recurring and one-time Personal Agent tasks, stored on the owner's agent row.

This module also holds the one scheduler of Personal Agent (sweep): it fires the due schedules and
considers the due proactive checks (heartbeat.py). Both keep their times in the agent's config;
PersonalAgent.next_due_at is the earliest of them, kept by store(), and is what the scheduler queries.
"""
import logging
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update

from personal_agent import access
from personal_agent.models import PersonalAgent

log = logging.getLogger(__name__)
MAX_SCHEDULES = 20
# Agents one sweep looks at, earliest first. The rest are due in the next sweep, 15 seconds later.
DUE_PER_SWEEP = 200
# How far apart sweeps are (background.SCHEDULE_INTERVAL_SEC): proactive checks over the limit of one sweep are spread by it.
SWEEP_SEC = 15
BACKFILL_BATCH = 200
# An agent whose scheduling raised is looked at again after this, not in every sweep.
ERROR_RETRY_SEC = 300
DEFAULT_TIMEZONE = "Asia/Shanghai"
MIN_REPEAT_SEC = 600
# Request keys of tasks created by a schedule: "schedule:<schedule id>:<occurrence epoch>".
SCHEDULE_KEY_PREFIX = "schedule:"

def is_unattended_run(task):
    """A task a schedule or a heartbeat started. It does the work now and does not manage schedules;
    its result may stay quiet when the check finds nothing."""
    if task is not None and (task.checkpoint or {}).get("continuation_seq"):
        return False
    from personal_agent.scheduling.heartbeat import is_beat_key
    key = (task.request_key or "") if task is not None else ""
    return key.startswith(SCHEDULE_KEY_PREFIX) or is_beat_key(key)

def is_proactive(task):
    """A task a heartbeat turn started and the user has not taken over (PROACTIVE_DESIGN §6).
    Nobody is there while it runs: narrower tools, every approval asked, no questions
    and a quiet failure. A follow-up from the user makes it an ordinary task again."""
    from personal_agent.scheduling.heartbeat import is_beat_key
    return task is not None and is_beat_key(task.request_key or "") and is_unattended_run(task)

def due_at(config):
    """The earliest time anything of this agent is due: an enabled schedule, or its proactive check once
    the agent is scheduled for them. None when nothing is."""
    config = config or {}
    times = [item["next_run"] for item in config.get("schedules", []) if item.get("enabled", True) and item.get("next_run")]
    beat = config.get("heartbeat") or {}
    if (beat.get("proactive") or {}).get("seeded") and beat.get("next_run"): times.append(beat["next_run"])
    return min(times) if times else None

def store(agent, config):
    """Write the agent's config together with next_due_at. Every write that may change a schedule's
    next_run or config["heartbeat"]["next_run"] goes through here; otherwise the scheduler does not see the new time."""
    agent.config = config
    agent.next_due_at = due_at(config)

def resync(db, agent, config):
    """End a look at an agent that had nothing due. next_due_at may have come apart from the config (a
    write that did not go through store: an older backend during a rolling release, new code): it is
    put right here, or the row would be read in every sweep and stand in front of the others."""
    if agent.next_due_at == due_at(config):
        db.rollback()
        return
    agent.next_due_at = due_at(config)
    db.commit()

def update_timed(db, uid, change):
    """identity.config.update_config for changes that may move a due time: read, change and write the
    config under the row lock, and keep next_due_at with it. Returns change's result."""
    from personal_agent.identity.config import locked_agent
    row = locked_agent(db, uid)
    config = dict(row.config or {})
    result = change(config)
    store(row, config)
    db.commit()
    return result

def zone(name):
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown timezone") from None

def user_zone(config):
    return (config or {}).get("timezone") or DEFAULT_TIMEZONE

def next_cron(cron, timezone, after):
    from features.automation.scheduling import compute_next_run
    return compute_next_run(cron, timezone, datetime.fromtimestamp(after, zone(timezone))).timestamp()

def next_time(item, after):
    """Return the next occurrence as an epoch, or None when a one-time item is spent."""
    if not item.get("enabled", True): return None
    if item.get("cron"): return next_cron(item["cron"], item["timezone"], after)
    return item["run_at"] if item.get("run_at", 0) > after else None

def validate(title, instruction, cron, run_at, timezone):
    if not title.strip() or len(title) > 100: raise ValueError("Title must contain 1 to 100 characters")
    if not instruction.strip() or len(instruction) > 4000: raise ValueError("Instruction must contain 1 to 4000 characters")
    if bool(cron) == bool(run_at): raise ValueError("Give either a cron expression or a run_at time")
    tz = zone(timezone)
    if cron:
        from croniter import croniter
        if not croniter.is_valid(cron) or len(cron.split()) != 5: raise ValueError("Use a five-field cron expression")
        first = next_cron(cron, timezone, time.time())
        if next_cron(cron, timezone, first) - first < MIN_REPEAT_SEC: raise ValueError("Recurring tasks must be at least ten minutes apart")
        return None
    try:
        moment = datetime.fromisoformat(run_at)
    except ValueError:
        raise ValueError("run_at must use ISO format, for example 2026-10-10T08:00") from None
    if moment.tzinfo is None: moment = moment.replace(tzinfo=tz)
    epoch = moment.timestamp()
    if epoch <= time.time(): raise ValueError("run_at must be in the future")
    return epoch

def view(item):
    tz = zone(item["timezone"])
    upcoming = item.get("next_run")
    return {"id": item["id"], "title": item["title"], "instruction": item["instruction"], "cron": item.get("cron"),
            "timezone": item["timezone"], "enabled": item.get("enabled", True), "revision": item.get("revision", 0),
            "run_at": datetime.fromtimestamp(item["run_at"], tz).isoformat() if item.get("run_at") else None,
            "next_run": datetime.fromtimestamp(upcoming, tz).isoformat() if upcoming else None}

def create(db, uid, title, instruction, cron="", run_at="", timezone=""):
    from personal_agent.identity.config import user_config
    timezone = timezone or user_zone(user_config(db, uid))
    epoch = validate(title, instruction, cron.strip(), run_at.strip(), timezone)

    def change(config):
        items = list(config.get("schedules", []))
        # The same timing and title is the same schedule. Return it instead of adding a copy.
        for existing in items:
            if existing.get("cron") == (cron.strip() or None) and existing.get("run_at") == epoch and existing["title"] == title.strip():
                return {**view(existing), "existing": True}
        if len(items) >= MAX_SCHEDULES: raise ValueError("Delete an old scheduled task first")
        item = {"id": "pa_sched_" + uuid.uuid4().hex[:16], "title": title.strip(), "instruction": instruction.strip(),
                "cron": cron.strip() or None, "run_at": epoch, "timezone": timezone, "created_at": time.time()}
        item["next_run"] = next_time(item, time.time())
        config["schedules"] = [*items, item]
        return view(item)
    return update_timed(db, uid, change)

def listing(config):
    return [view(item) for item in (config or {}).get("schedules", [])]

def update_schedule(db, uid, ident, patch):
    """Edit only this owner's schedule under the same lock used when firing it."""
    allowed = {"title", "instruction", "cron", "run_at", "timezone", "enabled", "revision"}
    if set(patch) - allowed or any(value is None for value in patch.values()):
        raise ValueError("Invalid schedule settings")

    def change(config):
        items = list(config.get("schedules", []))
        index = next((i for i, item in enumerate(items) if item["id"] == ident), None)
        if index is None: raise LookupError("Scheduled task not found")
        old = items[index]
        if "revision" in patch and patch["revision"] != old.get("revision", 0):
            raise ValueError("Schedule changed; reopen settings and try again")
        item = {**old, **{k: v for k, v in patch.items() if k not in {"run_at", "revision"}}}
        for key in ("title", "instruction", "timezone"):
            item[key] = item[key].strip()
        if not item["title"] or len(item["title"]) > 100: raise ValueError("Title must contain 1 to 100 characters")
        if not item["instruction"] or len(item["instruction"]) > 4000: raise ValueError("Instruction must contain 1 to 4000 characters")
        zone(item["timezone"])
        timing = bool({"cron", "run_at", "timezone"} & patch.keys())
        enabling = item.get("enabled", True) and not old.get("enabled", True)
        if timing or enabling:
            cron = (item.get("cron") or "").strip()
            run_at = patch.get("run_at", "")
            if run_at and "cron" not in patch: cron = ""
            if not cron and "run_at" not in patch and old.get("run_at"):
                # Preserve the local wall time when changing the one-time schedule's timezone.
                run_at = datetime.fromtimestamp(old["run_at"], zone(old["timezone"])).replace(tzinfo=None).isoformat()
            epoch = validate(item["title"], item["instruction"], cron, run_at, item["timezone"])
            item.update(cron=cron or None, run_at=epoch)
        item["revision"] = old.get("revision", 0) + 1
        if timing or enabling or not item.get("enabled", True):
            item["next_run"] = next_time(item, time.time())
        items[index] = item
        config["schedules"] = items
        return view(item)
    return update_timed(db, uid, change)

def delete(db, uid, ident):

    def change(config):
        items = config.get("schedules", [])
        kept = [item for item in items if item["id"] != ident]
        if len(kept) == len(items): raise LookupError("Scheduled task not found")
        config["schedules"] = kept
        return {"deleted": True}
    return update_timed(db, uid, change)

def fire_schedules(db, uid, now):
    """Fire the owner's due schedules and advance them, atomically under the owner's lock. The number of tasks started.
    An occurrence the owner may not run (no permission) is skipped, not kept for later."""
    from personal_agent.context import AgentContext
    from personal_agent.identity.config import locked_agent
    from personal_agent.tasks import task_ops
    agent = locked_agent(db, uid)
    config = dict(agent.config or {})
    is_due = lambda item: item.get("enabled", True) and item.get("next_run") and item["next_run"] <= now
    if not any(is_due(item) for item in config.get("schedules", [])):
        beat = config.get("heartbeat") or {}
        # With a proactive check due, the column is right; heartbeat.fire_one looks at the rest.
        if (beat.get("next_run") or now + 1) > now: resync(db, agent, config)
        else: db.rollback()
        return 0
    svc = AgentContext(db, uid)
    permitted = agent.status == "ready" and access.allowed(db, uid)
    updated, count = [], 0
    for item in config.get("schedules", []):
        if not is_due(item):
            updated.append(item)
            continue
        if permitted:
            key = SCHEDULE_KEY_PREFIX + item["id"] + ":" + str(int(item["next_run"]))
            goal = "【定时任务：" + item["title"] + "，现在执行】" + item["instruction"]
            message = svc.conversation.append(agent, "user", goal, None, key)
            message.extra_data = {**message.extra_data, "origin": "schedule"}
            task_ops.create(svc, message, goal, key, title=item["title"])
            count += 1
        item = {**item, "last_run": now, "next_run": next_time(item, now)}
        if item["next_run"] is not None: updated.append(item)
    config["schedules"] = updated
    store(agent, config)
    db.commit()
    return count

def sweep(db, now=None):
    """One pass of the scheduler: (tasks started by schedules, proactive checks admitted).

    The due agents come from the index on next_due_at; no config of an agent that is not due is read.
    A paused agent is not looked at: what came due while it was paused is considered once when it is ready again.
    Proactive checks are admitted up to heartbeat.admit_budget() per sweep: this process's share, less
    when checks already wait for the Router (every backend process sweeps on its own; the waiting checks
    are what they share). The ones over it are moved to the next sweeps, so that they do not stand in
    front of other agents' schedules. An agent whose scheduling raises is moved back a few minutes."""
    from personal_agent.scheduling import heartbeat
    now = now if now is not None else time.time()
    uids = db.scalars(select(PersonalAgent.user_id).where(PersonalAgent.status == "ready", PersonalAgent.next_due_at <= now)
        .order_by(PersonalAgent.next_due_at).limit(DUE_PER_SWEEP)).all()
    db.rollback()
    tasks = checks = held = 0
    per, budget = heartbeat.admit_per_cycle(), None
    for uid in uids:
        try:
            tasks += fire_schedules(db, uid, now)
            if budget is None:
                budget = heartbeat.admit_budget(db)
                db.rollback()
            hold = None
            if checks >= budget:
                # Not admitted in this sweep: `per` of them come due again in each of the next sweeps.
                held += 1
                hold = now + SWEEP_SEC * (1 + (held - 1) // per)
            checks += heartbeat.fire_one(db, uid, now, hold=hold)
        except Exception as exc:
            db.rollback()
            log.warning("Personal Agent scheduling failed (%s)", type(exc).__name__)
            _put_off(db, uid, now + ERROR_RETRY_SEC)
    return tasks, checks

def _put_off(db, uid, when):
    """Move only the column: the config keeps its times, and the agent is looked at again at `when`."""
    try:
        db.execute(update(PersonalAgent).where(PersonalAgent.user_id == str(uid)).values(next_due_at=when))
        db.commit()
    except Exception as exc:  # noqa: BLE001 - the next sweep tries again
        db.rollback()
        log.warning("Personal Agent scheduling not put off (%s)", type(exc).__name__)

def fire_due(db, now=None):
    """Run one sweep; the number of tasks that schedules started."""
    return sweep(db, now)[0]

def backfill(db):
    """Once, when the process starts: put next_due_at right on every row where it differs from the
    config, in batches until done. A database that got the column without the migration's backfill (the
    desktop database is reconciled, not migrated) would otherwise never fire the schedules it already
    holds; and a column that is later than the config (see resync) has no other way to be found: the
    scheduler never reads that row. This reads every agent's config once per start. Returns the rows changed."""
    cursor, changed = "", 0
    while True:
        rows = db.scalars(select(PersonalAgent).where(PersonalAgent.id > cursor)
            .order_by(PersonalAgent.id).limit(BACKFILL_BATCH).with_for_update(skip_locked=True)).all()
        for row in rows:
            when = due_at(row.config)
            if row.next_due_at != when:
                row.next_due_at = when
                changed += 1
        last = rows[-1].id if rows else cursor
        db.commit()
        if len(rows) < BACKFILL_BATCH: return changed
        cursor = last
