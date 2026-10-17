"""Heartbeat: the agent starts itself at an interval and checks the user's checklist.

The checklist is HEARTBEAT.md in the workspace. A due beat becomes one turn of the agent
in the main conversation (see front.py), like a finished task. The turn usually ends
quiet; it sends a short alert only when an item needs the user. Checks that need tools
(email, web, devices) go to one background task. Settings and state live in
config["heartbeat"]; there is no table of its own.
"""
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db.engine import SessionLocal
from core.db.models import ChatMessage
from core.db.repository.chat import ChatMessageRepository
from core.infra.time import utc_now
from personal_agent import access
from personal_agent.models import PersonalAgent, PersonalMessageInbox
from personal_agent.workspace.constants import HEARTBEAT_FILE as CHECKLIST

log = logging.getLogger(__name__)
KEY_PREFIX = "heartbeat:"
EVENT = "heartbeat"
CHECKLIST_MAX_CHARS = 4000
DEFAULT_MINUTES = 30
MAX_MINUTES = 1440
DEFAULT_HOURS = {"start": "08:00", "end": "22:00"}
SETTINGS = {"enabled", "every_minutes", "active_hours", "channel_id"}
# A beat that waited this long for its turn is out of date.
STALE_SEC = 600
BUSY_RETRY_SEC = 120
# Alerts of this window are shown to the beat and are not sent again.
ALERT_WINDOW_SEC = 86400
RECENT_ALERTS = 3
# Quiet beats leave an event row; it is removed after this time.
KEEP_QUIET_SEC = 86400
MANUAL_INTERVAL_SEC = 60
# A beat that cannot call pa_stay_quiet answers with this token instead (as in OpenClaw).
QUIET_TOKEN = "HEARTBEAT_OK"
OUTCOMES = ("quiet", "alert", "delegated", "duplicate", "failed", "stale")
SKIP_REASONS = {
    "paused": "Personal Agent is paused",
    "no_permission": "Personal Agent is not enabled for this account",
    "busy": "The agent is answering a message; try again in a moment",
    "empty_checklist": "HEARTBEAT.md has no items to check",
    "outside_hours": "Outside the active hours",
    "daily_limit": "The daily heartbeat limit is reached",
}

def server_enabled():
    return os.getenv("PERSONAL_AGENT_HEARTBEAT", "on").strip().lower() not in ("off", "false", "0", "no")

def _int_env(name, default, low):
    try: return max(low, int(os.getenv(name, str(default))))
    except ValueError: return default

def min_minutes():
    return _int_env("PERSONAL_AGENT_HEARTBEAT_MIN_MINUTES", 15, 1)

def daily_max():
    return _int_env("PERSONAL_AGENT_HEARTBEAT_DAILY_MAX", 48, 1)

def is_beat_key(key):
    return (key or "").startswith(KEY_PREFIX)

def settings(config):
    """The heartbeat settings and state with defaults."""
    stored = dict((config or {}).get("heartbeat") or {})
    return {"enabled": False, "every_minutes": DEFAULT_MINUTES, "active_hours": dict(DEFAULT_HOURS), "channel_id": None, **stored}

# ---------------------------------------------------------------- settings

def _minutes_of_day(value):
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value if isinstance(value, str) else "")
    if not match: raise ValueError("Use HH:MM for active hours")
    return int(match.group(1)) * 60 + int(match.group(2))

def validate(patch, config, now=None):
    """Merge a settings patch into config["heartbeat"] and return the new object."""
    now = now or time.time()
    if not isinstance(patch, dict): raise ValueError("Heartbeat settings must be an object")
    if set(patch) - SETTINGS: raise ValueError("Unknown heartbeat setting")
    current = settings(config)
    if "enabled" in patch and type(patch["enabled"]) is not bool: raise ValueError("enabled must be true or false")
    if "every_minutes" in patch:
        value = patch["every_minutes"]
        if type(value) is not int or not min_minutes() <= value <= MAX_MINUTES:
            raise ValueError("Heartbeat interval must be from " + str(min_minutes()) + " to " + str(MAX_MINUTES) + " minutes")
    if "active_hours" in patch:
        hours = patch["active_hours"]
        if not isinstance(hours, dict) or set(hours) != {"start", "end"}: raise ValueError("Active hours need start and end")
        for value in hours.values(): _minutes_of_day(value)
    if "channel_id" in patch:
        channel = patch["channel_id"] or None
        if channel is not None and channel not in (config or {}).get("channel_ids", []):
            raise ValueError("The bot is not connected to Personal Agent")
        patch = {**patch, "channel_id": channel}
    merged = {**current, **patch}
    if not merged["enabled"]:
        merged["next_run"] = None
    elif not current["enabled"] or "every_minutes" in patch or not merged.get("next_run"):
        # The first beat comes one interval after the user turns it on; "run now" is the quick test.
        merged["next_run"] = now + merged["every_minutes"] * 60
    return merged

def view(config):
    """Settings and the last outcome, for the page."""
    from personal_agent.scheduling.schedules import user_zone, zone
    hb = settings(config)
    tz = zone(user_zone(config))
    stamp = lambda epoch: datetime.fromtimestamp(epoch, tz).isoformat() if epoch else None
    last = hb.get("last") or {}
    return {"enabled": hb["enabled"], "every_minutes": hb["every_minutes"], "active_hours": hb["active_hours"],
            "channel_id": hb["channel_id"], "next_run": stamp(hb.get("next_run")) if hb["enabled"] else None,
            "last": {**last, "at": stamp(last.get("at"))} if last else None, "stats": hb.get("stats") or {},
            "min_minutes": min_minutes(), "daily_max": daily_max()}

# ---------------------------------------------------------------- time

def local(now, timezone):
    from personal_agent.scheduling.schedules import zone
    return datetime.fromtimestamp(now, zone(timezone))

def in_active_hours(hb, timezone, now):
    """True inside the window. An end before the start crosses midnight; equal ends mean all day."""
    start, end = _minutes_of_day(hb["active_hours"]["start"]), _minutes_of_day(hb["active_hours"]["end"])
    if start == end: return True
    moment = local(now, timezone)
    minute = moment.hour * 60 + moment.minute
    return start <= minute < end if start < end else minute >= start or minute < end

def next_window_start(hb, timezone, now):
    start = _minutes_of_day(hb["active_hours"]["start"])
    moment = local(now, timezone)
    candidate = moment.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if candidate <= moment: candidate += timedelta(days=1)
    return candidate.timestamp()

def local_day(timezone, now):
    return local(now, timezone).date().isoformat()

# ---------------------------------------------------------------- checklist

def checklist_items(text):
    """The checklist without comments, headings, blank lines and empty list items."""
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.DOTALL)
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"): continue
        if re.fullmatch(r"([-*+]|\d+[.)])(\s*\[[ xX]?\])?", stripped): continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()

def read_checklist(agent_id):
    from personal_agent.workspace.files import WorkspaceUnavailable
    from personal_agent.workspace.files import read_text
    try: return read_text(agent_id, CHECKLIST)
    except (WorkspaceUnavailable, OSError): return None

def ensure_checklist(agent_id, name=None):
    """Write the empty template when HEARTBEAT.md is missing. A current file is never replaced."""
    from personal_agent.workspace.constants import DEFAULT_NAME
    from personal_agent.workspace.files import WorkspaceUnavailable
    from personal_agent.workspace.persona import ensure_seeded
    from personal_agent.workspace.prompting import prompt_part
    from personal_agent.workspace.files import read_text
    from personal_agent.workspace.files import write_file
    try:
        ensure_seeded(agent_id)
        if read_text(agent_id, CHECKLIST) is not None: return False
        write_file(agent_id, CHECKLIST, prompt_part("template_heartbeat").replace("{name}", name or DEFAULT_NAME) + "\n", expected="")
        return True
    except (WorkspaceUnavailable, LookupError, ValueError, PermissionError, OSError) as exc:
        log.info("Heartbeat checklist not written (%s)", type(exc).__name__)
        return False

# ---------------------------------------------------------------- scheduling

def check(db, agent, hb, now, manual=False):
    """The reason to skip this beat, or None. No model call is made here."""
    from personal_agent.conversation.message_routing import OPEN
    from personal_agent.scheduling.schedules import user_zone
    timezone = user_zone(agent.config)
    if agent.status != "ready": return "paused"
    if not access.allowed(db, agent.user_id): return "no_permission"
    if not manual and not in_active_hours(hb, timezone, now): return "outside_hours"
    day = hb.get("day") or {}
    if not manual and day.get("date") == local_day(timezone, now) and day.get("beats", 0) >= daily_max(): return "daily_limit"
    if db.scalar(select(PersonalMessageInbox.message_id).where(PersonalMessageInbox.agent_id == agent.id,
            PersonalMessageInbox.status.in_(OPEN)).limit(1)): return "busy"
    if not checklist_items(read_checklist(agent.id)): return "empty_checklist"
    return None

def admit(db, agent, key, slot, now, manual=False):
    """The beat's event message and its inbox row. The unique request key admits one beat per slot."""
    from personal_agent.scheduling.schedules import user_zone
    label = local(now, user_zone(agent.config)).strftime("%Y-%m-%d %H:%M (%A)")
    message = ChatMessageRepository(db).create({"message_id": "pa_hb_" + uuid.uuid4().hex, "chat_id": agent.chat_id,
        "role": "system", "content": "[Heartbeat] " + label, "extra_data": {"event": EVENT, "slot": slot, "manual": manual}}, commit=False)
    db.add(PersonalMessageInbox(message_id=message.message_id, agent_id=agent.id, request_key=key[:100],
        payload={"kind": EVENT, "slot": slot, "manual": manual}))
    db.flush()
    return message.message_id

def _count(hb, timezone, now):
    today = local_day(timezone, now)
    day = hb.get("day") or {}
    hb["day"] = {"date": today, "beats": (day.get("beats", 0) if day.get("date") == today else 0) + 1}

def _record_skip(hb, reason, now):
    last = hb.get("last") or {}
    # One write per change of reason: an agent outside its hours does not write every minute.
    if last.get("outcome") != "skipped" or last.get("reason") != reason:
        hb["last"] = {"at": now, "outcome": "skipped", "reason": reason}

def fire_one(db, uid, now):
    """Admit the due beat of one owner, or record why it is skipped. True when admitted."""
    from personal_agent.scheduling.schedules import user_zone
    from personal_agent.identity.config import locked_agent
    agent = locked_agent(db, uid)
    config = dict(agent.config or {})
    hb = settings(config)
    if not hb["enabled"] or not hb.get("next_run") or hb["next_run"] > now:
        db.rollback()
        return False
    timezone, slot = user_zone(config), int(hb["next_run"])
    reason = check(db, agent, hb, now)
    every = hb["every_minutes"] * 60
    if reason:
        # A conversation in progress delays the beat a little; other reasons wait a full interval.
        hb["next_run"] = (next_window_start(hb, timezone, now) if reason == "outside_hours"
                          else now + min(BUSY_RETRY_SEC, every) if reason == "busy" else now + every)
        _record_skip(hb, reason, now)
        agent.config = {**config, "heartbeat": hb}
        db.commit()
        return False
    try:
        # Workers that read the same next_run build the same key; milliseconds keep close slots apart.
        admit(db, agent, KEY_PREFIX + str(int(hb["next_run"] * 1000)), slot, now)
    except IntegrityError:
        # Another worker admitted this slot.
        db.rollback()
        return False
    # Missed slots collapse into this one beat.
    hb["next_run"] = now + every
    _count(hb, timezone, now)
    agent.config = {**config, "heartbeat": hb}
    db.commit()
    return True

def fire_due(db, now=None):
    """Admit every due beat. Rows are read without locks; only owners with a due beat are locked."""
    if not server_enabled(): return 0
    now = now or time.time()
    fired = 0
    for uid, config in db.execute(select(PersonalAgent.user_id, PersonalAgent.config)).all():
        hb = (config or {}).get("heartbeat") or {}
        if not hb.get("enabled") or not hb.get("next_run") or hb["next_run"] > now: continue
        try:
            fired += fire_one(db, uid, now)
        except Exception as exc:
            db.rollback()
            log.warning("Heartbeat admission failed (%s)", type(exc).__name__)
    return fired

def run_now(db, uid, now=None):
    """Start one beat at once for the user. Active hours and the daily limit do not apply."""
    from personal_agent.scheduling.schedules import user_zone
    from personal_agent.identity.config import locked_agent
    now = now or time.time()
    agent = locked_agent(db, uid)
    config = dict(agent.config or {})
    hb = settings(config)
    if hb.get("manual_at") and now - hb["manual_at"] < MANUAL_INTERVAL_SEC:
        db.rollback()
        raise ValueError("Wait a minute before the next heartbeat")
    reason = check(db, agent, hb, now, manual=True)
    if reason:
        db.rollback()
        raise ValueError(SKIP_REASONS[reason])
    slot = int(now)
    ident = admit(db, agent, KEY_PREFIX + "manual:" + str(int(now * 1000)), slot, now, manual=True)
    hb["manual_at"] = now
    _count(hb, user_zone(config), now)
    agent.config = {**config, "heartbeat": hb}
    db.commit()
    return {"started": True, "message_id": ident}

def record(agent_id, outcome):
    """Store the outcome of a finished beat on the agent."""
    with SessionLocal() as db:
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.id == agent_id).with_for_update())
        if agent is None: return
        config = dict(agent.config or {})
        hb = settings(config)
        stats = dict(hb.get("stats") or {})
        stats[outcome] = stats.get(outcome, 0) + 1
        hb["stats"], hb["last"] = stats, {"at": time.time(), "outcome": outcome, "reason": None}
        agent.config = {**config, "heartbeat": hb}
        db.commit()

# ---------------------------------------------------------------- the beat turn

def is_stale(row, now=None):
    created = row.created_at
    if created is None: return False
    if created.tzinfo is None:
        from datetime import timezone
        created = created.replace(tzinfo=timezone.utc)
    return ((now or utc_now()) - created).total_seconds() > STALE_SEC

def _alerts(db, chat_id, now=None):
    """Alerts of the last day, newest first."""
    since = (now or utc_now()) - timedelta(seconds=ALERT_WINDOW_SEC)
    rows = db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.role == "assistant",
        ChatMessage.created_at >= since).order_by(ChatMessage.chat_seq.desc()).limit(200)).all()
    return [row for row in rows if (row.extra_data or {}).get(EVENT) and not (row.extra_data or {}).get("streaming")]

def is_quiet_text(text):
    """An empty reply, or one that is only the quiet token."""
    return not (text or "").replace(QUIET_TOKEN, "").strip(" \n\t.。!！")

def normalize(text):
    return re.sub(r"[\W_]+", "", text or "").lower()

def is_duplicate(db, chat_id, text, exclude=None):
    target = normalize(text)
    return bool(target) and any(normalize(row.content) == target for row in _alerts(db, chat_id) if row.message_id != exclude)

def prompt_block(db, agent):
    """The beat's rules, the user's checklist and the recent alerts."""
    from personal_agent.workspace.prompting import _truncate
    from personal_agent.workspace.prompting import prompt_part
    from personal_agent.scheduling.schedules import user_zone, zone
    items = checklist_items(read_checklist(agent.id)) or "(empty)"
    body, _ = _truncate(items, CHECKLIST_MAX_CHARS)
    lines = [prompt_part("front_event_heartbeat"), "", "# HEARTBEAT.md (the user's checklist)", body]
    alerts = _alerts(db, agent.chat_id)[:RECENT_ALERTS]
    tz = zone(user_zone(agent.config))
    lines += ["", "# Your recent heartbeat alerts (do not repeat them)"]
    lines += ["- " + row.created_at.astimezone(tz).strftime("%m-%d %H:%M") + ": " + row.content[:300].replace("\n", " ")
              for row in alerts] or ["(none)"]
    return "\n".join(lines)

def cleanup(db, now=None):
    """Remove the event rows of quiet beats after a day. Their inbox rows go with them."""
    cutoff = (now or utc_now()) - timedelta(seconds=KEEP_QUIET_SEC)
    rows = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.request_key.startswith(KEY_PREFIX),
        PersonalMessageInbox.status.in_(("done", "failed")), PersonalMessageInbox.created_at < cutoff).limit(500)).all()
    removed = 0
    for row in rows:
        decision = row.decision or {}
        if not decision.get("quiet") or decision.get("work_item_ids"): continue
        message = db.get(ChatMessage, row.message_id)
        db.delete(row)
        if message is not None: db.delete(message)
        removed += 1
    db.commit()
    return removed
