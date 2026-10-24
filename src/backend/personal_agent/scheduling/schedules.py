"""Recurring and one-time Personal Agent tasks, stored on the owner's agent row."""
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from personal_agent import access
from personal_agent.models import PersonalAgent

MAX_SCHEDULES = 20
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
    from personal_agent.identity.config import update_config
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
    return update_config(db, uid, change)

def listing(config):
    return [view(item) for item in (config or {}).get("schedules", [])]

def update_schedule(db, uid, ident, patch):
    """Edit only this owner's schedule under the same lock used when firing it."""
    from personal_agent.identity.config import update_config
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
    return update_config(db, uid, change)

def delete(db, uid, ident):
    from personal_agent.identity.config import update_config

    def change(config):
        items = config.get("schedules", [])
        kept = [item for item in items if item["id"] != ident]
        if len(kept) == len(items): raise LookupError("Scheduled task not found")
        config["schedules"] = kept
        return {"deleted": True}
    return update_config(db, uid, change)

def fire_due(db, now=None):
    """Read the current settings, enqueue and advance atomically under the owner's lock."""
    from personal_agent.context import AgentContext
    from personal_agent.identity.config import update_config
    from personal_agent.tasks import task_ops
    now = now if now is not None else time.time()
    fired = 0
    owners = [uid for uid, config in db.execute(select(PersonalAgent.user_id, PersonalAgent.config)).all()
              if any(item.get("enabled", True) and item.get("next_run") and item["next_run"] <= now
                     for item in (config or {}).get("schedules", []))]
    for uid in owners:
        def advance(config):
            svc = AgentContext(db, uid)
            agent = svc.identity.agent()
            permitted = agent.status == "ready" and access.allowed(db, uid)
            updated, count = [], 0
            for item in config.get("schedules", []):
                if not item.get("enabled", True) or not item.get("next_run") or item["next_run"] > now:
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
            return count
        fired += update_config(db, uid, advance)
    return fired
