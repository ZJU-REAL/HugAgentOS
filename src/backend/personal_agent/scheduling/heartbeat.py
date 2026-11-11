"""Proactive checks: the agent goes through the user's list in the morning, in the evening and when the user is idle.

The list is HEARTBEAT.md in the workspace. A due check becomes one turn of the agent in the main
conversation (see conversation/router/), like a finished task. Before it, the platform looks for
leads in the database (proactive_probe.py). The turn usually ends quiet; items that need tools go
to one background task, whose result comes back as one summary. Settings and state live in
config["heartbeat"]; there is no table of its own. There is no loop of its own either: the scheduler of
scheduling/schedules.py (sweep) finds the agents whose check is due and calls fire_one. The names "heartbeat" and HEARTBEAT.md are the
older names of the same thing (PROACTIVE_DESIGN.md).
"""
import hashlib
import logging
import os
import re
import stat
import time
import uuid
from datetime import datetime, timedelta, timezone as utc_zone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.db.engine import SessionLocal
from core.db.models import ChatMessage, ChatRun, ChatSession
from core.db.repository.chat import ChatMessageRepository
from core.infra.time import utc_now
from personal_agent import access
from personal_agent.models import PersonalAgent, PersonalMessageInbox, PersonalWorkItem
from personal_agent.workspace.constants import HEARTBEAT_FILE as CHECKLIST

log = logging.getLogger(__name__)
KEY_PREFIX = "heartbeat:"
EVENT = "heartbeat"
CHECKLIST_MAX_CHARS = 4000
DEFAULT_HOURS = {"start": "08:30", "end": "20:00"}
DEFAULT_IDLE_MINUTES = 120
MAX_IDLE_MINUTES = 720
# One default time must not start every check in the same minute: each agent has a fixed offset below this.
JITTER_SEC = 1800
# A window missed by more than this is dropped, not made up later.
GRACE_SEC = 7200
# No idle check this close to the evening one.
IDLE_GAP_SEC = 3600
# Admissions of one window; see settle_proactive.
MAX_ATTEMPTS = 3
# An admitted check that was never settled is forgotten after this.
PENDING_MAX_SEC = 1800
SKIP_RETRY_SEC = 1800
BUSY_RETRY_SEC = 120
# With the deployment switch off, an agent is looked at again after this.
SWITCH_RETRY_SEC = 3600
# A check whose agent changed between the look and the lock is looked at again in the next sweep.
LOOK_AGAIN_SEC = 15
# How far back the owner's activity is looked for: an idle check asks about the last hours only.
ACTIVE_LOOKBACK_SEC = 43200
SEED_BATCH = 200
# "enabled" and "every_minutes" are accepted from older clients and ignored.
SETTINGS = {"enabled", "every_minutes", "active_hours", "channel_id"}
IGNORED_SETTINGS = {"enabled", "every_minutes"}
# channel_id: None sends to the bot of the owner's latest direct chat; this value keeps messages on the web.
WEB_ONLY = "none"
# A check that waited this long for its turn is out of date.
STALE_SEC = 600
# Messages of this window are shown to the check and are not sent again.
ALERT_WINDOW_SEC = 86400
RECENT_ALERTS = 3
# Quiet checks leave an event row; it is removed after this time.
KEEP_QUIET_SEC = 86400
MANUAL_INTERVAL_SEC = 60
# A check that cannot call pa_stay_quiet answers with this token instead (as in OpenClaw).
QUIET_TOKEN = "HEARTBEAT_OK"
SKIP_REASONS = {
    "paused": "Personal Agent is paused",
    "no_permission": "Personal Agent is not enabled for this account",
    "busy": "The agent is answering a message; try again in a moment",
    "empty_checklist": "HEARTBEAT.md has no items to check",
    "proactive_running": "The previous proactive check has not ended",
    "daily_limit": "The daily limit of proactive checks is reached",
}
# How a check's own turn can end; a turn outcome closes the admitted check (settle_proactive).
TURN_OUTCOMES = {"quiet", "alert", "delegated", "duplicate", "failed", "stale"}
# What the task of a check can add later: a summary was sent, nothing was found, the task failed.
TASK_OUTCOMES = {"reported", "nothing", "task_failed"}
# The heading of the untouched empty template of the older heartbeat.
LEGACY_HEADING = "# HEARTBEAT.md - 心跳清单"
TOLD_KEEP_SEC = 14 * 86400
TOLD_MAX = 100
# News has its own share: web addresses never push the other matters out.
NEWS_MAX = 40
TOLD_SHOWN = 40
# A matter told is not raised again for this long; after it, once more while it is still there.
RETELL_SEC = 3 * 86400
MAX_TIMES = 3
# Refs the probe named, kept a week for the weekly audit to compare with.
LEADS_KEEP_SEC = 7 * 86400
LEADS_MAX = 200
# A summary this long after an audit was admitted still counts as that audit's.
AUDIT_OPEN_SEC = 6 * 3600
# Drafts of proactive tasks (/workspace/drafts/) are removed after this.
DRAFTS_DIR = "drafts"
DRAFT_KEEP_SEC = 14 * 86400
DRAFT_SWEEP_SEC = 6 * 3600
# Added once to an agent's first summary. It also reaches IM, so it is text, not a UI key.
FIRST_NOTE = "这是我每天早晚主动为你留意后发现的事。想调整我留意什么，直接告诉我，或在设置里改主动服务清单。"
FIRST_NOTE_NEWS = "告诉我你平时关注什么，我每天早上帮你留意相关的新动态。"
FOCUS = {
    "morning": "Morning proactive check: what came in overnight, what is due today.",
    "evening": "Evening proactive check: what is still open from today, what to prepare for tomorrow.",
    "idle": "Idle proactive check: only what is new since the last proactive check.",
    "manual": "The user started this proactive check by hand: go through the whole list now.",
    "audit": "Weekly full proactive check: no leads are given this time. Go through the whole list as in the evening, "
             "review the chats listed below and look at the mail of the last 14 days.",
}

def server_enabled():
    return os.getenv("PERSONAL_AGENT_HEARTBEAT", "on").strip().lower() not in ("off", "false", "0", "no")

def _int_env(name, default, low):
    try: return max(low, int(os.getenv(name, str(default))))
    except ValueError: return default

def daily_max():
    """A backstop: no agent is admitted more often than this in one day."""
    return _int_env("PERSONAL_AGENT_HEARTBEAT_DAILY_MAX", 48, 1)

def idle_minutes():
    """Minutes without a check and without the user before an idle check. 0 turns idle checks off."""
    return min(MAX_IDLE_MINUTES, _int_env("PERSONAL_AGENT_PROACTIVE_IDLE_MINUTES", DEFAULT_IDLE_MINUTES, 0))

def idle_daily_max():
    return _int_env("PERSONAL_AGENT_PROACTIVE_IDLE_DAILY_MAX", 2, 0)

def admit_per_cycle():
    """Checks one backend process admits in one sweep of the scheduler (every 15 seconds)."""
    return _int_env("PERSONAL_AGENT_PROACTIVE_ADMIT_PER_CYCLE", 15, 1)

def max_waiting():
    """Checks that may wait for the Router at one time, over all agents and all backend processes.
    Each process admits on its own; this is what they share. More than the Router ends within the
    ten minutes a check may wait (STALE_SEC) would only be dropped and tried again."""
    return _int_env("PERSONAL_AGENT_PROACTIVE_MAX_WAITING", 40, 1)

def admit_budget(db):
    """How many checks this sweep may admit: its own share, less when checks already wait for the Router."""
    from personal_agent.conversation.router.triggers import OPEN
    waiting = db.scalar(select(func.count()).select_from(PersonalMessageInbox).where(
        PersonalMessageInbox.kind == EVENT, PersonalMessageInbox.status.in_(OPEN))) or 0
    return max(0, min(admit_per_cycle(), max_waiting() - waiting))


def is_beat_key(key):
    return (key or "").startswith(KEY_PREFIX)

def _store(agent, config):
    """Every write of config["heartbeat"] goes through schedules.store: next_run moves the agent's next_due_at."""
    from personal_agent.scheduling.schedules import store
    store(agent, config)

def _dt(epoch):
    return datetime.fromtimestamp(epoch, utc_zone.utc)

def _epoch(moment):
    if moment is None: return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=utc_zone.utc)).timestamp()

# ---------------------------------------------------------------- settings

def _minutes_of_day(value):
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value if isinstance(value, str) else "")
    if not match: raise ValueError("Use HH:MM for the morning and evening times")
    return int(match.group(1)) * 60 + int(match.group(2))

def _usable_hours(hours):
    try: return _minutes_of_day(hours["start"]) != _minutes_of_day(hours["end"])
    except (ValueError, KeyError, TypeError): return False

def settings(config):
    """The settings and state with defaults. There is no switch: every Personal Agent has proactive checks.
    "enabled" and "every_minutes" of the older heartbeat may still be stored; they are not read."""
    stored = dict((config or {}).get("heartbeat") or {})
    hb = {"active_hours": dict(DEFAULT_HOURS), "channel_id": None, **stored}
    # Equal times meant "all day" to the older heartbeat. A check needs two different times.
    if not _usable_hours(hb["active_hours"]): hb["active_hours"] = dict(DEFAULT_HOURS)
    return hb

def validate(patch, config, now=None):
    """Merge a settings patch into config["heartbeat"] and return the new object."""
    now = now or time.time()
    if not isinstance(patch, dict): raise ValueError("Proactive settings must be an object")
    if set(patch) - SETTINGS: raise ValueError("Unknown proactive setting")
    patch = {key: value for key, value in patch.items() if key not in IGNORED_SETTINGS}
    current = settings(config)
    if "active_hours" in patch:
        hours = patch["active_hours"]
        if not isinstance(hours, dict) or set(hours) != {"start", "end"}: raise ValueError("Give a morning and an evening time")
        if _minutes_of_day(hours["start"]) == _minutes_of_day(hours["end"]): raise ValueError("The morning and evening times must differ")
    if "channel_id" in patch:
        channel = patch["channel_id"] or None
        if channel not in (None, WEB_ONLY) and channel not in (config or {}).get("channel_ids", []):
            raise ValueError("The bot is not connected to Personal Agent")
        patch = {**patch, "channel_id": channel}
    merged = {**current, **patch}
    # New times: the scheduler looks again at once instead of at the time it worked out from the old ones.
    if "active_hours" in patch and merged.get("next_run"): merged["next_run"] = now
    return merged

_unavailable_cache = {}

def unavailable(db, agent):
    """What the list asks for but cannot be done now: "email" (an email item, no connected mailbox),
    "channel" (no direct IM chat to send to: summaries stay on the web)."""
    cached = _unavailable_cache.get(agent.id)
    if cached and time.monotonic() - cached[0] < 30: return list(cached[1])
    from integrations.email.models import EmailConnection
    from personal_agent.channels.channel_bridge import home_origin
    config, missing = agent.config or {}, []
    if re.search(r"邮箱|邮件|e-?mail", checklist_items(read_checklist(agent.id)), re.IGNORECASE):
        row = db.get(EmailConnection, agent.user_id) if config.get("email_enabled") else None
        if not row or row.status != "connected": missing.append("email")
    # "Web only" is the user's choice, not something missing.
    if settings(config)["channel_id"] != WEB_ONLY and home_origin(db, agent.chat_id) is None: missing.append("channel")
    if len(_unavailable_cache) > 2000: _unavailable_cache.clear()
    _unavailable_cache[agent.id] = (time.monotonic(), missing)
    return list(missing)

def view(config, db=None, agent=None):
    """Settings, state and the last outcome, for the page. "unavailable" needs db and the agent row."""
    from personal_agent.scheduling.schedules import user_zone, zone
    hb = settings(config)
    tz = zone(user_zone(config))
    stamp = lambda epoch: datetime.fromtimestamp(epoch, tz).isoformat() if epoch else None
    last = hb.get("last") or {}
    state = {key: value for key, value in (hb.get("proactive") or {}).items() if key not in ("leads", "lead_names")}
    return {"active_hours": hb["active_hours"], "channel_id": hb["channel_id"], "next_run": stamp(hb.get("next_run")),
            "last": {**last, "at": stamp(last.get("at"))} if last else None, "stats": hb.get("stats") or {},
            "proactive": {**state, "backoff": backoff(state)},
            "unavailable": unavailable(db, agent) if db is not None and agent is not None else []}

# ---------------------------------------------------------------- time

def local(now, timezone):
    from personal_agent.scheduling.schedules import zone
    return datetime.fromtimestamp(now, zone(timezone))

def local_day(timezone, now):
    return local(now, timezone).date().isoformat()

def jitter(agent_id):
    """A fixed offset per agent, so that one default time does not start every check in the same minute."""
    return int(hashlib.sha256(agent_id.encode()).hexdigest()[:8], 16) % JITTER_SEC

def audit_day(agent_id):
    """The weekday (0 Monday to 4 Friday) whose evening check is the agent's weekly audit."""
    return int(hashlib.sha256(("audit:" + agent_id).encode()).hexdigest()[:8], 16) % 5

def windows(hb, timezone, now, offset):
    """(day, morning, evening): the proactive day that now falls in, and its two times as epochs."""
    start, end = _minutes_of_day(hb["active_hours"]["start"]), _minutes_of_day(hb["active_hours"]["end"])
    moment = local(now - offset, timezone)
    morning = moment.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if morning > moment: morning -= timedelta(days=1)
    evening = morning.replace(hour=end // 60, minute=end % 60)
    if evening <= morning: evening += timedelta(days=1)      # an end before the start is on the next day
    return morning.date().isoformat(), morning.timestamp() + offset, evening.timestamp() + offset

def backoff(state):
    """0: all checks. 1: no idle checks. 2: the morning check only. From the summaries the user did not answer."""
    ignored = (state or {}).get("ignored", 0)
    return 2 if ignored >= 6 else 1 if ignored >= 3 else 0

DAY_KEYS = ("morning", "evening", "idle", "attempts")

def _new_day(state, day):
    """The state at the start of a proactive day: the counts start again; what was reviewed, told and ignored carries over."""
    return {**{key: value for key, value in state.items() if key not in DAY_KEYS}, "day": day, "idle": 0}

def next_morning(timezone, morning, offset):
    """The morning time of the next local day. A calendar day, not 24 hours: the day the clocks change
    is an hour longer or shorter."""
    return (local(morning - offset, timezone) + timedelta(days=1)).timestamp() + offset

def due(hb, timezone, now, active, offset, audit=None):
    """(window, next_check, state). window is the check to consider now, or None; next_check is when to
    look again. active is the epoch of the owner's last activity; audit is the weekday whose evening
    check is the weekly audit. No model call and no query here."""
    day, morning, evening = windows(hb, timezone, now, offset)
    state = dict(hb.get("proactive") or {})
    if state.get("day") != day: state = _new_day(state, day)
    pending = state.get("pending")
    if pending and now - pending["at"] < PENDING_MAX_SEC: return None, now + 60, state   # not ended yet
    state.pop("pending", None)
    attempts = state.get("attempts") or {}
    tomorrow = next_morning(timezone, morning, offset)
    level = backoff(state)
    open_ = lambda window: not state.get(window) and attempts.get(window, 0) < MAX_ATTEMPTS
    if open_("morning") and now < morning + GRACE_SEC: return "morning", now, state
    if now >= evening:
        if open_("evening") and now < evening + GRACE_SEC and level < 2:
            weekly = audit is not None and local(evening - offset, timezone).weekday() == audit
            return ("audit" if weekly else "evening"), now, state
        return None, tomorrow, state
    idle = idle_minutes() * 60
    if (not idle or level >= 1 or state.get("idle", 0) >= idle_daily_max()
            or attempts.get("idle", 0) >= idle_daily_max() + MAX_ATTEMPTS): return None, evening, state
    ready = max(state.get("last_at") or 0, morning, active or 0) + idle
    if ready > evening - IDLE_GAP_SEC: return None, evening, state
    if ready > now: return None, ready, state
    return "idle", now, state

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

def ensure_checklist(agent_id, name=None, first=False):
    """Write the default list when HEARTBEAT.md is missing; with first, also when it is still the untouched
    empty template of the older heartbeat. A list the user changed or emptied is never replaced.
    True when written, False when left as it is, None when the workspace could not be reached.

    Called once, when the scheduler first looks at the agent (_before_lock): an empty list is how the user stops
    the checks, so it must not be filled again later. It opens its own sessions: call it without a row lock."""
    from personal_agent.workspace.constants import DEFAULT_NAME
    from personal_agent.workspace.files import WorkspaceUnavailable
    from personal_agent.workspace.persona import ensure_seeded
    from personal_agent.workspace.prompting import prompt_part
    from personal_agent.workspace.files import read_file
    from personal_agent.workspace.files import write_file
    try:
        ensure_seeded(agent_id)
        template = prompt_part("template_heartbeat").replace("{name}", name or DEFAULT_NAME) + "\n"
        # read_file raises LookupError for a missing file.
        try: current = read_file(agent_id, CHECKLIST)
        except LookupError: current = None
        if current is None:
            write_file(agent_id, CHECKLIST, template, expected="")
            return True
        if not first: return False
        body = re.sub(r"<!--.*?-->", "", current["content"] or "", flags=re.DOTALL).strip()
        if body != LEGACY_HEADING: return False
        write_file(agent_id, CHECKLIST, template, expected=current["etag"])
        return True
    except ValueError:
        # The file changed while it was looked at (another worker seeding it, or the user saving): nothing is
        # decided yet. The next cycle reads it again; a list the user wrote is then left as it is.
        return None
    except (WorkspaceUnavailable, LookupError, PermissionError, OSError) as exc:
        log.info("Proactive list not written (%s)", type(exc).__name__)
        return None

# ---------------------------------------------------------------- scheduling

def last_active_at(db, agent, now=None):
    """When the owner last did something in the last hours: wrote to the agent, or started a run in an
    ordinary chat. Runs the agent itself started do not count: its own conversation and task chats are
    reserved chats, and the runs its tasks start in the owner's chats are known by their operations."""
    from personal_agent.scheduling.proactive_probe import agent_runs, own_runs
    now = now or time.time()
    since = _dt(now - ACTIVE_LOOKBACK_SEC)
    wrote = db.scalar(select(func.max(PersonalMessageInbox.created_at)).where(PersonalMessageInbox.agent_id == agent.id,
        PersonalMessageInbox.kind == "message", PersonalMessageInbox.created_at >= since))
    ran = db.scalar(select(func.max(ChatRun.created_at)).join(ChatSession, ChatSession.chat_id == ChatRun.chat_id)
        .where(own_runs(agent.user_id, agent_runs(db, agent.user_id, now - ACTIVE_LOOKBACK_SEC)), ChatRun.created_at >= since))
    stamps = [_epoch(stamp) for stamp in (wrote, ran) if stamp]
    return max(stamps) if stamps else None

def check(db, agent, hb, now, manual=False):
    """The reason to skip this check, or None. No model call is made here. The time is not judged here: due() does."""
    from personal_agent.conversation.message_routing import OPEN
    from personal_agent.scheduling.schedules import user_zone
    timezone = user_zone(agent.config)
    if agent.status != "ready": return "paused"
    if not access.allowed(db, agent.user_id): return "no_permission"
    day = hb.get("day") or {}
    if not manual and day.get("date") == local_day(timezone, now) and day.get("beats", 0) >= daily_max(): return "daily_limit"
    if db.scalar(select(PersonalMessageInbox.message_id).where(PersonalMessageInbox.agent_id == agent.id,
            PersonalMessageInbox.status.in_(OPEN)).limit(1)): return "busy"
    # The task of the previous check is still at work. One that waits for the user (an approval) does not
    # hold the next check back: it may wait for days, and the wait itself is a lead (proactive_probe).
    if db.scalar(select(PersonalWorkItem.id).where(PersonalWorkItem.agent_id == agent.id,
            PersonalWorkItem.request_key.like(KEY_PREFIX + "%"),
            PersonalWorkItem.status.in_(("queued", "running", "computer_wait"))).limit(1)): return "proactive_running"
    if not checklist_items(read_checklist(agent.id)): return "empty_checklist"
    return None

def admit(db, agent, key, slot, now, manual=False, window=None, found=None):
    """The check's event message and its inbox row. The unique request key admits one check per key."""
    from personal_agent.scheduling.schedules import user_zone
    label = local(now, user_zone(agent.config)).strftime("%Y-%m-%d %H:%M (%A)") + (" " + window if window else "")
    message = ChatMessageRepository(db).create({"message_id": "pa_hb_" + uuid.uuid4().hex, "chat_id": agent.chat_id,
        "role": "system", "content": "[Heartbeat] " + label,
        "extra_data": {"event": EVENT, "slot": slot, "manual": manual, "window": window}}, commit=False)
    from personal_agent.conversation.router.triggers import enqueue
    enqueue(db, agent, kind="heartbeat", message=message, request_key=key,
            payload={"kind": EVENT, "slot": slot, "manual": manual, "window": window, "found": found})
    db.flush()
    return message.message_id

def _count(hb, timezone, now):
    today = local_day(timezone, now)
    day = hb.get("day") or {}
    hb["day"] = {"date": today, "beats": (day.get("beats", 0) if day.get("date") == today else 0) + 1}

def _record_skip(hb, reason, now):
    last = hb.get("last") or {}
    # One write per change of reason: an agent that stays busy does not rewrite it every time.
    if last.get("outcome") != "skipped" or last.get("reason") != reason:
        hb["last"] = {"at": now, "outcome": "skipped", "reason": reason}

def _remember_mail(state, found):
    """Keep the highest uid of the untold mail these leads show. A check has now been asked about
    that mail; only mail after it starts an idle check (proactive_probe.probe)."""
    uids = ((found or {}).get("untold") or {}).get("uids") or []
    if uids: state["mail_seen"] = max([state.get("mail_seen") or 0, *uids])

def _remember_leads(state, found, now):
    """Keep the refs the probe named for a week: the weekly audit counts what was in neither them nor told."""
    from personal_agent.scheduling.proactive_probe import refs_of
    leads = {ref: at for ref, at in (state.get("leads") or {}).items() if now - at < LEADS_KEEP_SEC}
    leads.update({ref: now for ref in (found["nominated"] if "nominated" in found else refs_of(found))})
    state["leads"] = dict(sorted(leads.items(), key=lambda item: item[1])[-LEADS_MAX:])
    # The titles of this check's leads: a summary that names a title told that matter, tag or no tag.
    state["lead_names"] = {item["ref"]: item["title"] for items in found.values() if isinstance(items, list)
                           for item in items if isinstance(item, dict) and item.get("ref") and item.get("title")}

def _wrote_since(db, agent, sent):
    """Whether the owner wrote to the agent after the summary sent at `sent`."""
    return bool(db.scalar(select(PersonalMessageInbox.message_id).where(PersonalMessageInbox.agent_id == agent.id,
        PersonalMessageInbox.kind == "message", PersonalMessageInbox.created_at > _dt(sent)).limit(1)))

def _decide(hb, timezone, now, active, agent_id, wrote=None):
    """(window, next_check, ask): what is due now, with the owner's answer to the last summary taken in.
    No query here. ask is True when the answer is needed and wrote is not given: get _wrote_since and
    call again. Changes hb in place.

    The last summary is judged once, at an admission: answered (the count of unanswered summaries is
    cleared) or not (one more). A later message still clears the count, also at a look that admits
    nothing because the backoff holds the check back: the owner's answer brings it back at once, and
    a summary is not counted as unanswered there."""
    plan = lambda: due(hb, timezone, now, active, jitter(agent_id), audit_day(agent_id))
    window, next_check, hb["proactive"] = plan()
    state = hb["proactive"]
    sent, judged, before = state.get("reported_at"), state.get("judged"), state.get("ignored", 0)
    if not sent or (judged and not before) or not (window or backoff(state)): return window, next_check, False
    if wrote is None: return window, next_check, True
    if wrote:
        state["ignored"] = 0
        if not judged: hb["stats"] = {**(hb.get("stats") or {}), "responded": (hb.get("stats") or {}).get("responded", 0) + 1}
    elif not window: return window, next_check, False
    elif not judged: state["ignored"] = before + 1
    state["judged"] = True
    # An answered summary lifts the backoff, an unanswered one adds to it: both change what is due.
    if state["ignored"] != before: window, next_check, hb["proactive"] = plan()
    return window, next_check, False

def _mark_listed(db, uid):
    from personal_agent.identity.config import locked_agent
    agent = locked_agent(db, uid)
    config = dict(agent.config or {})
    hb = settings(config)
    hb["proactive"] = {**(hb.get("proactive") or {}), "listed": True}
    _store(agent, {**config, "heartbeat": hb})
    db.commit()

def _ensure_listed(db, uid, agent_id, name):
    """Write the default list of an agent that has none yet. Its own sessions: call it without a row lock."""
    try: written = ensure_checklist(agent_id, name, first=True)
    except Exception as exc:  # noqa: BLE001 - a list that cannot be written now is written at the next look
        log.info("Proactive list not written (%s)", type(exc).__name__)
        written = None
    if written is not None: _mark_listed(db, uid)

def _scheduled(hb, now):
    return bool((hb.get("proactive") or {}).get("seeded") and hb.get("next_run") and hb["next_run"] <= now)

def _stamp(window, state):
    """What the look decided on. The lock compares it: an agent that changed in between is looked at again."""
    return (window, state.get("day"), state.get("last_at"), state.get("chats_at"), state.get("ignored", 0))

def has_news(text):
    """Whether the list asks for news: its checks look on the internet, and their summaries give web addresses."""
    return bool(re.search(r"资讯|新闻|news", checklist_items(text), re.IGNORECASE))

def _leads(db, agent, hb, window, now, reason):
    """The leads of a check that may run. The mailbox is a network call with the owner's credentials: it
    is read only for a check that passed check() (never for an account without permission or with an
    empty list, and not at every retry of a busy agent), and with no transaction open."""
    from personal_agent.scheduling import proactive_probe
    if reason: return None
    account = proactive_probe.mail_account(db, agent)
    db.rollback()
    # The mail the user already heard of, by uid: the mailbox says which unanswered mail is not among it.
    told = {item["ref"][6:] for item in _told(hb, now) if item["ref"].startswith("email:")}
    mail = (proactive_probe.mail_counts(account, (hb.get("proactive") or {}).get("last_at") or now - 86400, now, told)
            if account else (None, None))
    return proactive_probe.probe(db, agent, hb, window, now, mail)

def _look(db, uid, now, hold=None):
    """Everything a due check needs to read, before the agent's row is locked: the owner's activity and
    answer, the reason not to run, the leads. The Router takes the same row lock for every turn of the
    owner; it must not wait for these queries, a file and a mailbox. None when no check is due.
    With hold (the sweep has admitted its share) only the time is decided."""
    from personal_agent.scheduling.schedules import user_zone
    agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == str(uid)).execution_options(populate_existing=True))
    config = dict(agent.config or {}) if agent is not None else {}
    hb = settings(config)
    if agent is None or not _scheduled(hb, now) or not server_enabled():
        db.rollback()
        return None
    agent_id, name, timezone = agent.id, config.get("name"), user_zone(config)
    listed, sent = (hb.get("proactive") or {}).get("listed"), (hb.get("proactive") or {}).get("reported_at")
    active = last_active_at(db, agent, now)
    window, _, ask = _decide(hb, timezone, now, active, agent_id)
    wrote = None
    if ask:
        wrote = _wrote_since(db, agent, sent)
        hb = settings(config)
        window, _, _ = _decide(hb, timezone, now, active, agent_id, wrote)
    look = {"active": active, "sent": sent, "wrote": wrote, "stamp": _stamp(window, hb["proactive"]),
            "reason": None, "found": None, "links": False}
    # No transaction stays open over the file and network calls below.
    db.rollback()
    if not listed: _ensure_listed(db, uid, agent_id, name)
    if window is None or hold is not None: return look
    look["reason"] = check(db, agent, hb, now)
    look["found"] = _leads(db, agent, hb, window, now, look["reason"])
    look["links"] = window == "morning" or has_news(read_checklist(agent_id))
    db.rollback()
    return look

def _mark_ran(db, uid, day, window, now):
    """The key of this window exists already: the window ran before the times or the timezone were
    changed (a change can open the same day's window again). It counts as done. Whatever is stored,
    next_run moves on: a key that exists is never tried again in the next sweep."""
    from personal_agent.identity.config import locked_agent
    agent = locked_agent(db, uid)
    config = dict(agent.config or {})
    hb = settings(config)
    state = dict(hb.get("proactive") or {})
    if state.get("day") != day: state = _new_day(state, day)
    if window == "idle": state["idle"] = state.get("idle", 0) + 1
    else: state["evening" if window == "audit" else window] = True
    hb["proactive"], hb["next_run"] = state, now + 60
    _store(agent, {**config, "heartbeat": hb})
    db.commit()

def fire_one(db, uid, now, hold=None):
    """Consider the check of one owner: admit it, or store why not and when to look again. True when admitted.
    hold is set when this sweep has admitted its share: a check that is due is not admitted and is
    looked at again at that time. Called by the scheduler (schedules.sweep) for every due agent.

    What is read is read before the lock (_look); under the lock only next_run, pending and what the
    look decided on are checked again, and the admission is written."""
    from personal_agent.identity.config import locked_agent
    from personal_agent.scheduling.schedules import resync, user_zone
    look = _look(db, uid, now, hold)
    agent = locked_agent(db, uid)
    config = dict(agent.config or {})
    hb = settings(config)
    if not _scheduled(hb, now):
        # Nothing is due here. A column that says otherwise is put right, or the row would be read in every sweep.
        resync(db, agent, config)
        return False
    timezone = user_zone(config)

    def save(when, reason=None):
        hb["next_run"] = when
        if reason: _record_skip(hb, reason, now)
        _store(agent, {**config, "heartbeat": hb})
        db.commit()

    if not server_enabled():
        # The deployment switch is off: the agent keeps its state and is looked at again later.
        save(now + SWITCH_RETRY_SEC)
        return False
    if look is None or (hb.get("proactive") or {}).get("reported_at") != look["sent"]:
        save(now + LOOK_AGAIN_SEC)
        return False
    window, next_check, ask = _decide(hb, timezone, now, look["active"], agent.id, look["wrote"])
    state = hb["proactive"]
    if window is None:
        # Never at or before now, whatever due() worked out: a time in the past would bring the agent back in every sweep.
        save(max(next_check, now + 60))
        return False
    if ask or _stamp(window, state) != look["stamp"]:
        save(now + LOOK_AGAIN_SEC)
        return False
    if hold is not None:
        save(hold)
        return False
    reason, found = look["reason"], look["found"]
    if reason:
        # A conversation or a task in progress delays the check a little; the window's grace decides whether it still runs.
        save(now + (BUSY_RETRY_SEC if reason in ("busy", "proactive_running") else SKIP_RETRY_SEC), reason)
        return False
    if window == "idle" and not found["changed"]:
        # Nothing new happened: no model call. What only persists waits for the morning or evening check.
        # Look again after one more idle period; this does not count as a check.
        state["last_at"] = now
        hb["stats"] = {**(hb.get("stats") or {}), "probe_skipped": (hb.get("stats") or {}).get("probe_skipped", 0) + 1}
        # The next time is worked out again from here: one more idle period, or the evening check when
        # that comes first (now + the idle period alone would run past it).
        later = due(hb, timezone, now, look["active"], jitter(agent.id), audit_day(agent.id))[1]
        save(max(later, now + 60))
        return False
    # The weekly audit takes the place of that day's evening check.
    slot = "evening" if window == "audit" else window
    attempts = state.get("attempts") or {}
    attempt = attempts.get(slot, 0) + 1
    count = state.get("idle", 0) + 1 if window == "idle" else 0
    # The same day, window and attempt build the same key on every worker: one admission each.
    key = KEY_PREFIX + ":".join([state["day"], window, str(count), str(attempt)])
    try:
        admit(db, agent, key, int(now), now, window=window, found=found)
    except IntegrityError:
        db.rollback()
        _mark_ran(db, uid, state["day"], window, now)
        return False
    # The window is marked only when its turn ends (settle_proactive): a check that is dropped or fails is tried again.
    state["pending"] = {"window": window, "attempt": attempt, "at": now}
    state["attempts"] = {**attempts, slot: attempt}
    _remember_leads(state, found, now)
    _remember_mail(state, found)
    state["links"] = look["links"]
    # Only the summary of the audit itself is compared with the week's leads (remember_told).
    if window == "audit": state["audit_at"] = now
    else: state.pop("audit_at", None)
    _count(hb, timezone, now)
    save(now + 60)
    return True

def fire_due(db, now=None):
    """Run one sweep of the scheduler; the number of proactive checks admitted. There is no loop for
    proactive checks alone: the due agents are found by schedules.sweep, through the index on next_due_at."""
    from personal_agent.scheduling.schedules import sweep
    return sweep(db, now)[1]

def is_seeded(config):
    return bool((((config or {}).get("heartbeat") or {}).get("proactive") or {}).get("seeded"))

def seed_one(db, uid, now=None):
    """Schedule an agent for proactive checks: set next_run and mark the windows already past as done.
    True when seeded. New and old agents take the same path; an older "enabled: false" does not count,
    the switch is gone. The default list is written when the agent is first looked at (_before_lock)."""
    from personal_agent.identity.config import locked_agent
    from personal_agent.scheduling.schedules import user_zone
    now = now or time.time()
    agent = locked_agent(db, uid)
    config = dict(agent.config or {})
    hb = settings(config)
    state = dict(hb.get("proactive") or {})
    if state.get("seeded") or agent.status != "ready" or not access.allowed(db, agent.user_id):
        db.rollback()
        return False
    day, _, evening = windows(hb, user_zone(config), now, jitter(agent.id))
    # No check right at the start: the windows already past count as done. The first check is the next window.
    hb["proactive"] = {**state, "day": day, "idle": 0, "morning": True, "evening": now >= evening, "last_at": now, "seeded": True}
    # Straight to the time of the next look: agents seeded together at a start are not all due at once,
    # in front of other agents' schedules.
    window, next_check, _ = due(hb, user_zone(config), now, None, jitter(agent.id), audit_day(agent.id))
    hb["next_run"] = now if window else next_check
    _store(agent, {**config, "heartbeat": hb})
    db.commit()
    return True

def seed_if_new(db, agent):
    """Seed the agent when it was never scheduled. For the moments an agent becomes usable: it is created,
    or its owner comes back to one that could not be seeded before (paused, no permission).
    An agent that is seeded costs no query here."""
    if is_seeded(agent.config) or agent.status != "ready": return False
    return seed_one(db, agent.user_id)

def seed_all(db, now=None):
    """Once, when the process starts: seed the agents that existed before proactive checks did, in
    batches until done. This is not a periodic task. An agent that cannot be seeded now is seeded when
    its owner next uses it (seed_if_new) or at the next start.
    Returns the number seeded."""
    if not server_enabled(): return 0
    unseeded = PersonalAgent.config[("heartbeat", "proactive", "seeded")].as_boolean().isnot(True)
    cursor, seeded = "", 0
    while True:
        rows = db.execute(select(PersonalAgent.user_id, PersonalAgent.id).where(PersonalAgent.status == "ready", unseeded,
            PersonalAgent.user_id > cursor).order_by(PersonalAgent.user_id).limit(SEED_BATCH)).all()
        db.rollback()
        for uid, agent_id in rows:
            try:
                seeded += seed_one(db, uid, now)
            except Exception as exc:
                db.rollback()
                log.warning("Proactive seeding failed (%s)", type(exc).__name__)
        if len(rows) < SEED_BATCH: return seeded
        cursor = rows[-1].user_id

def run_now(db, uid, now=None):
    """Start one check at once for the user. The times and the daily limit do not apply."""
    from personal_agent.scheduling.schedules import user_zone
    from personal_agent.identity.config import locked_agent
    now = now or time.time()

    def too_soon(hb):
        return bool(hb.get("manual_at") and now - hb["manual_at"] < MANUAL_INTERVAL_SEC)

    # As for a scheduled check: what is read is read before the row is locked.
    agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == str(uid)).execution_options(populate_existing=True))
    if agent is None: raise LookupError("Personal Agent not found")
    config = dict(agent.config or {})
    hb = settings(config)
    agent_id, name, state = agent.id, config.get("name"), hb.get("proactive") or {}
    if too_soon(hb):
        db.rollback()
        raise ValueError("Wait a minute before the next proactive check")
    db.rollback()
    if state.get("seeded") and not state.get("listed"): _ensure_listed(db, uid, agent_id, name)
    reason = check(db, agent, hb, now, manual=True)
    if reason:
        db.rollback()
        raise ValueError(SKIP_REASONS[reason])
    found = _leads(db, agent, hb, "manual", now, None)
    links = has_news(read_checklist(agent_id))
    db.rollback()
    agent = locked_agent(db, uid)
    config = dict(agent.config or {})
    hb = settings(config)
    if too_soon(hb):
        db.rollback()
        raise ValueError("Wait a minute before the next proactive check")
    ident = admit(db, agent, KEY_PREFIX + "manual:" + str(int(now * 1000)), int(now), now, manual=True, window="manual", found=found)
    state = dict(hb.get("proactive") or {})
    state["pending"] = {"window": "manual", "attempt": 1, "at": now}
    state.pop("audit_at", None)
    _remember_leads(state, found, now)
    _remember_mail(state, found)
    state["links"] = links
    hb["proactive"], hb["manual_at"] = state, now
    _count(hb, user_zone(config), now)
    _store(agent, {**config, "heartbeat": hb})
    db.commit()
    return {"started": True, "message_id": ident}

def settle_proactive(hb, outcome, now):
    """Close the admitted check and return the new state. A check that was dropped or failed leaves its
    window open: due() offers it again while its grace lasts and attempts remain."""
    state = dict(hb.get("proactive") or {})
    pending = state.pop("pending", None)
    if not pending or outcome in ("stale", "failed"): return state
    window, at = pending.get("window"), pending.get("at") or now
    # The time the leads were read: the next probe looks for what is new since then.
    state["last_at"] = at
    if window == "idle": state["idle"] = state.get("idle", 0) + 1
    elif window in ("morning", "evening"): state[window] = True
    elif window == "audit": state["evening"] = True
    if window in ("evening", "audit", "manual"): state["chats_at"] = at
    return state

def record(agent_id, outcome, now=None):
    """Store how a check ended. outcome is how its own turn ended (TURN_OUTCOMES; this closes the admitted
    check), or what its task added later (TASK_OUTCOMES)."""
    now = now or time.time()
    with SessionLocal() as db:
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.id == agent_id).with_for_update())
        if agent is None: return
        config = dict(agent.config or {})
        hb = settings(config)
        stats = dict(hb.get("stats") or {})
        stats[outcome] = stats.get(outcome, 0) + 1
        state = dict(hb.get("proactive") or {})
        if outcome in TURN_OUTCOMES and state.get("pending"):
            window = state["pending"].get("window")
            state = settle_proactive(hb, outcome, now)
            if window == "audit" and outcome not in ("stale", "failed"): stats["audit_runs"] = stats.get("audit_runs", 0) + 1
            # The next cycle decides what follows: the same window again, or the time of the next one.
            if state.get("seeded"): hb["next_run"] = now
        if outcome in ("alert", "reported"):
            # A summary went out: the next admission looks whether the owner answered it (_note_response).
            state["reported_at"], state["judged"] = now, False
        elif outcome != "delegated":
            # The check ended without a summary: an audit has nothing to compare (remember_told).
            state.pop("audit_at", None)
        if state: hb["proactive"] = state
        hb["stats"], hb["last"] = stats, {"at": now, "outcome": outcome, "reason": None}
        _store(agent, {**config, "heartbeat": hb})
        db.commit()

# ---------------------------------------------------------------- what the user was told

# A tag that names a matter, as models really write them: "(chat:c_1:179)", "(ref: chat_1)", "（run_7f）".
# An id is ASCII; without "ref" in front it carries a digit, or after "kind:" also "_" or "/".
_ID = r"[!-'*-~]{1,300}"
_KINDS = r"(?:task|approval|run|chat|email|news|pa|msg)"
KNOWN_TAG = re.compile(r"^[ \t]*[(（][ \t]*(?:ref[ \t]*[:：]?[ \t]*(" + _KINDS + "[:_]" + _ID + ")"
                       r"|((?:task|approval|run|chat|email|news):(?=[!-'*-~]*[\d_/])" + _ID + ")"
                       r"|(" + _KINDS + r"_(?=[!-'*-~]*\d)" + _ID + r"))[ \t]*[)）][ \t]*$", re.MULTILINE)
# A line that is only a tag, whatever it names. Models make tags up ("(ref: drafts)", "(ref: device:dev_1)",
# "(security:mail)"): every line that is "(ref ...)" or "(word:id)" with an ASCII id is taken out of what
# the user reads, and only a KNOWN_TAG is a matter that was told. "(run_once)", "（task:清理）" and an
# address in brackets stay: they are text.
TAG_LINE = re.compile(r"^[ \t]*[(（][ \t]*(?:ref\b[^()（）\n]{0,300}|(?!https?[:：])[a-z][a-z_]{1,19}[:：]" + _ID
                      + "|" + _KINDS + r"_(?=[!-'*-~]*\d)" + _ID + r")[ \t]*[)）][ \t]*$", re.MULTILINE)
# "(ref: ...)" inside a sentence is a tag too. Other brackets inside a sentence are the model's prose.
INLINE_TAG = re.compile(r"[ \t]*[(（][ \t]*ref[ \t]*[:：][^()（）\n]{0,300}[)）]")

def take_refs(text):
    """(text without its tags, the matters the tags name). No tag reaches the user.

    A tag that names a matter is taken as the model wrote it: the exact (kind:id) the platform gave, or
    a looser form such as (ref: <id>); remember_told resolves the loose ones against the leads of the
    check. A tag the model made up is removed and names nothing."""
    text, refs = text or "", []

    def drop(match):
        known = KNOWN_TAG.match(match.group(0).strip())
        if known: refs.append(next(group for group in known.groups() if group).strip())
        return ""
    clean = INLINE_TAG.sub(drop, TAG_LINE.sub(drop, text))
    if clean == text: return text.strip(), []
    return re.sub(r"\n{3,}", "\n\n", clean).strip(), refs

REF_KINDS = ("task", "approval", "run", "chat", "email", "news")

def resolve_refs(refs, leads):
    """The refs to store for the tags of a summary. An exact ref of a known kind stays. A loose tag (an id
    without its kind, a chat without its time) becomes the lead that carries that id; one that matches
    no lead is dropped."""
    out = []
    for ref in refs:
        if ref in leads or ref.split(":", 1)[0] in ("email", "news"):
            out.append(ref)
            continue
        ident = ref.split(":", 1)[1] if ref.split(":", 1)[0] in REF_KINDS and ":" in ref else ref
        ident = ident.split(":", 1)[0]
        matches = [lead for lead in leads if ident and ident in lead.split(":")]
        out += matches or ([ref] if ref.split(":", 1)[0] in REF_KINDS else [])
    return list(dict.fromkeys(out))

def _told(hb, now):
    """The entries still kept: {"ref", "at", "times"}."""
    return [item for item in hb.get("told") or [] if now - item.get("at", 0) < TOLD_KEEP_SEC]

def hidden_refs(hb, now):
    """Refs not to raise now: told within the last 3 days, or told 3 times already. News is told once."""
    return {item["ref"] for item in _told(hb, now)
            if now - item["at"] < RETELL_SEC or item.get("times", 1) >= MAX_TIMES or item["ref"].startswith("news:")}

def named_refs(text, names):
    """The leads whose title the summary names. Models leave the tags out often enough that the tags
    alone cannot be what decides whether a matter was told. Only the title in its brackets counts, as
    the leads show it to the model (「title」): the bare words of a title are ordinary text. A title
    that several leads share names none of them."""
    names, text = names or {}, text or ""
    shared = {title for title in names.values() if list(names.values()).count(title) > 1}
    return [ref for ref, title in names.items() if title not in shared and "「" + title + "」" in text]

URL = re.compile(r"https?://[^\s<>\"'，。；、！？：（）【】《》「」]+")

TRACKING = re.compile(r"^(?:utm_\w+|fbclid|gclid|spm)$", re.IGNORECASE)

def _addresses(text):
    """The web addresses in a text, as written, without the punctuation after them."""
    out = []
    for url in URL.findall(text or ""):
        while url and (url[-1] in ".,;:!?'\"*_~]}>" or (url[-1] == ")" and url.count(")") > url.count("("))): url = url[:-1]
        if len(url) > len("https://"): out.append(url)
    return out

def same_page(url):
    """One spelling for the addresses of one page: https, the host in lower case, no fragment, no
    tracking parameters, no slash at the end. The same news item comes back from a search with
    another spelling of its address; it is still the item the user already read."""
    from urllib.parse import urlsplit
    try: parts = urlsplit(url)
    except ValueError: return url[:300]
    if not parts.netloc: return url[:300]
    query = "&".join(pair for pair in parts.query.split("&") if pair and not TRACKING.match(pair.split("=", 1)[0]))
    return ("https://" + parts.netloc.lower() + parts.path.rstrip("/") + ("?" + query if query else ""))[:300]

def news_ref(ref):
    """A ref with a news ref's address in its one spelling; any other ref unchanged."""
    return "news:" + same_page(ref[5:]) if ref.startswith("news:") else ref

def linked_refs(text):
    """A news ref for every web address the summary gives. News is found on the internet by the model
    itself and written straight into the summary, without a tag for each item; the address is what
    names it."""
    return list(dict.fromkeys("news:" + same_page(url) for url in _addresses(text)))

def told_news(hb, now=None):
    """The addresses (same_page) of the news already sent."""
    return {same_page(item["ref"][5:]) for item in _told(hb, now or time.time()) if item["ref"].startswith("news:")}

def without_told_news(text, told):
    """(text, addresses dropped): a summary without the news items the user already got. told is told_news().

    The model is shown what was told and still sends an item again (found in live runs: the task that
    searches does not see that list). An item is a paragraph, or one line of a list; it goes when it
    gives web addresses and every one of them was already sent. A line that ends on a colon and is
    left with nothing under it goes too."""
    if not told or not text: return text or "", []
    dropped, blocks = [], []
    for block in re.split(r"\n[ \t]*\n", text):
        lines = block.split("\n")
        listed = sum(1 for line in lines if re.match(r"[ \t]*(?:[-*•]|\d+[.、)])[ \t]", line)) > 0
        parts = lines if listed else [block]
        kept = []
        for part in parts:
            urls = [same_page(url) for url in _addresses(part)]
            if urls and all(url in told for url in urls): dropped += urls
            else: kept.append(part)
        if len(kept) < len(parts):
            while kept and kept[-1].rstrip().endswith((":", "：")): kept.pop()
        if kept: blocks.append("\n".join(kept))
    if not dropped: return text, []
    return "\n\n".join(blocks).strip(), list(dict.fromkeys(dropped))

def remember_told(agent_id, refs, now=None, text=None):
    """Store the matters of a summary that was sent: a new ref is added with times 1; a known one gets the
    new time and times + 1. Entries older than TOLD_KEEP_SEC go; the latest TOLD_MAX stay. The summary
    after a weekly audit also counts the refs that neither the week's leads nor the told matters knew.
    text is the summary as sent: the leads it names by 「title」 are told too, and so are the web
    addresses it gives when its check looked for news."""
    now = now or time.time()
    refs = list(dict.fromkeys(ref for ref in refs or [] if isinstance(ref, str) and ref))
    if not refs and not text: return
    with SessionLocal() as db:
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.id == agent_id).with_for_update())
        if agent is None: return
        config = dict(agent.config or {})
        hb = settings(config)
        # Entries stored before same_page() keep their time and count under the one spelling.
        told = {news_ref(item["ref"]): {**item, "ref": news_ref(item["ref"])} for item in _told(hb, now)}
        state = dict(hb.get("proactive") or {})
        names = state.get("lead_names") or {}
        # Web addresses are news only where the check looks for news (state["links"], set at its admission):
        # elsewhere a link is a link to one of the user's own chats or to a document.
        links = linked_refs(text) if state.get("links") else []
        refs = [news_ref(ref) for ref in resolve_refs(refs, state.get("leads") or {}) + named_refs(text, names)] + links
        refs = list(dict.fromkeys(refs))
        audit = state.pop("audit_at", None)
        if not refs and audit is None: return
        if audit and now - audit < AUDIT_OPEN_SEC:
            known = set(told) | set(state.get("leads") or {})
            # News comes from the internet, not from the platform's leads: it is never a miss of the rules.
            missed = [ref for ref in refs if ref not in known and not ref.startswith("news:")]
            hb["stats"] = {**(hb.get("stats") or {}), "audit_new": (hb.get("stats") or {}).get("audit_new", 0) + len(missed)}
        for ref in refs:
            told[ref] = _entry(told.get(ref), ref, now, (told.get(ref) or {}).get("times", 0) + 1, names.get(ref))
        hb["told"] = _kept(told)
        if state: hb["proactive"] = state
        _store(agent, {**config, "heartbeat": hb})
        db.commit()

def _kept(told):
    """The entries to store, oldest first: the latest TOLD_MAX matters and the latest NEWS_MAX news."""
    items = sorted(told.values(), key=lambda item: item["at"])
    news = [item for item in items if item["ref"].startswith("news:")][-NEWS_MAX:]
    rest = [item for item in items if not item["ref"].startswith("news:")][-TOLD_MAX:]
    return sorted(rest + news, key=lambda item: item["at"])

def _entry(known, ref, now, times, title=None):
    """One told matter. The title of its lead is kept when there is one: it is how the matter is shown
    when the user asks not to hear of it again (told_list)."""
    title = title or (known or {}).get("title")
    return {"ref": ref, "at": now, "times": times, **({"title": title} if title else {})}

def told_list(config, now=None):
    """The matters told and still kept, newest first: {"ref", "at", "times", "title"?, "dismissed"}.
    For the tool that lets the user say which one not to raise again (dismiss_told)."""
    now = now or time.time()
    return [{**item, "dismissed": item.get("times", 1) >= MAX_TIMES}
            for item in sorted(_told(settings(config), now), key=lambda item: item["at"], reverse=True)]

def dismiss_told(agent_id, refs, now=None):
    """The user does not want to hear of these matters again: each is stored as told MAX_TIMES times, so
    that neither the leads nor the told block raise it until its entry goes (TOLD_KEEP_SEC from now).
    A ref is taken as remember_told takes a tag: exact, or an id that one told matter or lead carries.
    A whole kind of matter is not dismissed here: that is an edit of the list. Returns the refs dismissed."""
    now = now or time.time()
    if isinstance(refs, str): refs = [refs]
    refs = [ref.strip().strip("()（）") for ref in refs or [] if isinstance(ref, str) and ref.strip()]
    if not refs: return []
    with SessionLocal() as db:
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.id == agent_id).with_for_update())
        if agent is None: return []
        config = dict(agent.config or {})
        hb = settings(config)
        told = {item["ref"]: dict(item) for item in _told(hb, now)}
        state = hb.get("proactive") or {}
        refs = resolve_refs(refs, {**(state.get("leads") or {}), **told})
        for ref in refs:
            told[ref] = _entry(told.get(ref), ref, now, MAX_TIMES, (state.get("lead_names") or {}).get(ref))
        if refs:
            hb["told"] = _kept(told)
            _store(agent, {**config, "heartbeat": hb})
        db.commit()
        return refs

def told_block(db, agent, now=None):
    """The matters already told, for the check's turn and for the turn that reports its task."""
    from personal_agent.scheduling.schedules import user_zone, zone
    now = now or time.time()
    hb = settings(agent.config)
    tz, hide = zone(user_zone(agent.config)), hidden_refs(hb, now)
    quiet, again, news = [], [], []
    told = sorted(_told(hb, now), key=lambda item: item["at"], reverse=True)
    # News has its own lines: the other matters must not push an address out of what the model is shown.
    for item in [item for item in told if not item["ref"].startswith("news:")][:TOLD_SHOWN] + [item for item in told if item["ref"].startswith("news:")]:
        line = "- " + item["ref"] + " (told " + datetime.fromtimestamp(item["at"], tz).strftime("%m-%d") + ")"
        (news if item["ref"].startswith("news:") else quiet if item["ref"] in hide else again).append(line)
    lines = ["# Already told", "Do not raise these matters now:", *(quiet or ["(none)"])]
    if news:
        lines += ["News already sent. Never send an item with one of these addresses again:", *news]
    if again:
        lines += ["Told more than 3 days ago. Raise one of these again only when it is still open, and say that you raised it before:", *again]
    return "\n".join(lines)

_introduced = set()

def first_summary_note(agent_id):
    """The sentence that explains the first summary an agent sends, or None. It is returned once per agent.

    The turn that settles a summary calls this inside its own transaction. So it never waits for the
    agent's row (a locked row means no note this time; the next summary gets it), and it ends with a
    commit, never a rollback."""
    if agent_id in _introduced: return None
    # Without a news item the user is not asked what they follow.
    news = "资讯" in checklist_items(read_checklist(agent_id))
    with SessionLocal() as db:
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.id == agent_id).with_for_update(skip_locked=True))
        note = None
        if agent is not None:
            config = dict(agent.config or {})
            hb = settings(config)
            state = dict(hb.get("proactive") or {})
            if not state.get("introduced"):
                hb["proactive"] = {**state, "introduced": True}
                _store(agent, {**config, "heartbeat": hb})
                note = FIRST_NOTE + (FIRST_NOTE_NEWS if news else "")
            if len(_introduced) > 10000: _introduced.clear()
            _introduced.add(agent_id)
        db.commit()
    return note

# ---------------------------------------------------------------- the check's turn

def is_stale(row, now=None):
    created = row.created_at
    if created is None: return False
    if created.tzinfo is None: created = created.replace(tzinfo=utc_zone.utc)
    return ((now or utc_now()) - created).total_seconds() > STALE_SEC

def _alerts(db, chat_id, now=None):
    """Proactive messages of the last day, newest first: a check's own, and summaries of its task."""
    since = (now or utc_now()) - timedelta(seconds=ALERT_WINDOW_SEC)
    rows = db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.role == "assistant",
        ChatMessage.created_at >= since).order_by(ChatMessage.chat_seq.desc()).limit(200)).all()
    return [row for row in rows if (row.extra_data or {}).get(EVENT) and not (row.extra_data or {}).get("streaming")]

def is_quiet_text(text):
    """An empty reply, or one that is only the quiet token."""
    return not (text or "").replace(QUIET_TOKEN, "").strip(" \n\t.。!！")

def normalize(text):
    # The sentence added to an agent's first summary is not part of what the summary says.
    for note in (FIRST_NOTE_NEWS, FIRST_NOTE): text = (text or "").replace(note, "")
    return re.sub(r"[\W_]+", "", text).lower()

def is_duplicate(db, chat_id, text, exclude=None):
    target = normalize(text)
    return bool(target) and any(normalize(row.content) == target for row in _alerts(db, chat_id) if row.message_id != exclude)

LAST_WORD = ("# Before you write\n"
             "A news item is sent only with the full URL of its source page at its end. No URL, no item.\n"
             "An item you would tell the user to ignore is not sent.\n"
             "Internal ids (chat, run and task ids) appear only in tag lines, never in a sentence.")

def prompt_block(db, agent, payload=None):
    """The check's rules, what this window is about, the user's list, the platform's leads, the matters
    already told and the recent proactive messages. payload is the Trigger's: its window and leads."""
    from personal_agent.scheduling import proactive_probe
    from personal_agent.workspace.prompting import _truncate
    from personal_agent.workspace.prompting import prompt_part
    from personal_agent.scheduling.schedules import user_zone, zone
    payload = payload or {}
    window, found = payload.get("window"), payload.get("found")
    tz = zone(user_zone(agent.config))
    items = checklist_items(read_checklist(agent.id)) or "(empty)"
    body, _ = _truncate(items, CHECKLIST_MAX_CHARS)
    lines = [prompt_part("front_event_heartbeat")]
    if FOCUS.get(window): lines += ["", FOCUS[window]]
    lines += ["", "# HEARTBEAT.md (the user's proactive list)", body]
    # The weekly audit measures what the leads miss: it gets none, only the chats to review.
    if found and window == "audit": lines += ["", proactive_probe.render_candidates(found, tz)]
    elif found: lines += ["", proactive_probe.render(found, tz)]
    lines += ["", told_block(db, agent)]
    alerts = _alerts(db, agent.chat_id)[:RECENT_ALERTS]
    lines += ["", "# Your recent proactive messages (do not repeat them)"]
    lines += ["- " + row.created_at.astimezone(tz).strftime("%m-%d %H:%M") + ": " + row.content[:300].replace("\n", " ")
              for row in alerts] or ["(none)"]
    # Said last, where it is read last: found in live runs, a rule in the middle of the block is not kept.
    lines += ["", LAST_WORD]
    return "\n".join(lines)

# ---------------------------------------------------------------- cleanup

def _sweep(fd, cutoff, depth=0):
    """Remove the files older than cutoff below the open directory fd. Links are removed, never followed."""
    removed = 0
    for name in os.listdir(fd):
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if depth >= 4: continue
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try: removed += _sweep(sub, cutoff, depth + 1)
                finally: os.close(sub)
            elif info.st_mtime < cutoff:
                os.unlink(name, dir_fd=fd)
                removed += 1
        except OSError:
            continue
    return removed

def clean_drafts(agent_id, now=None):
    """Remove one agent's drafts of proactive tasks (/workspace/drafts/) that are older than 14 days."""
    from personal_agent.workspace.files import WorkspaceUnavailable, _open_dir, local_workspace
    try:
        fd = _open_dir(local_workspace(agent_id), [DRAFTS_DIR])
    except (WorkspaceUnavailable, OSError):
        return 0
    try: return _sweep(fd, (now or time.time()) - DRAFT_KEEP_SEC)
    finally: os.close(fd)

_drafts_swept = 0.0

def sweep_drafts(now=None):
    """clean_drafts for every workspace on this backend's volume, a few times a day."""
    global _drafts_swept
    from personal_agent.workspace.files import WorkspaceUnavailable, local_workspace
    if time.monotonic() - _drafts_swept < DRAFT_SWEEP_SEC and _drafts_swept: return 0
    _drafts_swept = time.monotonic()
    try:
        root = local_workspace("x").parent.parent
        names = [entry.name for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except (WorkspaceUnavailable, OSError):
        return 0
    return sum(clean_drafts(name, now) for name in names)

def cleanup(db, now=None):
    """Remove the event rows of quiet checks after a day; their inbox rows go with them. Old drafts go too.
    Returns the number of event rows removed."""
    cutoff = (now or utc_now()) - timedelta(seconds=KEEP_QUIET_SEC)
    rows = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.kind == EVENT,
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
    try:
        sweep_drafts()
    except Exception as exc:  # noqa: BLE001 - old drafts never stop the cleanup of event rows
        log.warning("Draft cleanup failed (%s)", type(exc).__name__)
    return removed
