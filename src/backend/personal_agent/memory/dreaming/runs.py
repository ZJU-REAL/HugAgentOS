"""Dreaming run state: settings, the daily slot, durable run rows and the memory lease.

A run is a system ChatMessage in the agent's own chat whose extra_data[KEY] holds its
status (pending / running / retry / failed / done), attempts and outcome; the agent's
config["dreaming_state"] points at the current one. The run's lease is the memory lease
(memory.state): claiming a run takes it, every model step and tool call renews it, and
online writes and editor saves are refused while it is held.
"""
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select

from core.db.models import ChatMessage
from personal_agent import access
from personal_agent.memory import state as memory_state
from personal_agent.models import PersonalAgent
from personal_agent.scheduling.schedules import user_zone, zone

KEY = "personal_dreaming"
MAX_ATTEMPTS = 5
RECEIPT = "personal_file_memory"   # workspace.file_memory.KEY: the journal receipt


class LeaseLost(RuntimeError):
    """The run no longer owns the memory lease, or the agent was paused mid-run."""


def options(config):
    """The nightly slot and the model. Dreaming always runs: there is no switch to turn it off
    (an "enabled" key older configs carry is ignored)."""
    stored = {k: v for k, v in ((config or {}).get("dreaming") or {}).items() if k != "enabled"}
    return {"hour": 3, "model_provider_id": None, **stored}


def validate(patch, config):
    """Merge a settings patch; model_provider_id arrives already resolved by the caller."""
    if not isinstance(patch, dict) or set(patch) - {"hour", "model_provider_id"}:
        raise ValueError("Unknown Dreaming setting")
    settings = {**options(config), **patch}
    if type(settings["hour"]) is not int or not 0 <= settings["hour"] <= 23:
        raise ValueError("Invalid Dreaming setting")
    model = settings["model_provider_id"]
    if model is not None and (not isinstance(model, str) or not 0 < len(model) <= 64):
        raise ValueError("Invalid Dreaming model")
    return settings


def provider(config):
    """The cheaper Dreaming model when set, else the agent's own model."""
    return options(config).get("model_provider_id") or (config or {}).get("model_provider_id")


def schedule(config, now):
    local = datetime.fromtimestamp(now, zone(user_zone(config)))
    slot = local.replace(hour=options(config)["hour"], minute=0, second=0, microsecond=0)
    if slot > local: slot -= timedelta(days=1)
    return slot.timestamp(), (slot + timedelta(days=1)).timestamp()


def locked(db, ident):
    return db.scalar(select(PersonalAgent).where(PersonalAgent.id == ident).with_for_update().execution_options(populate_existing=True))


def current(db, agent):
    ident = ((agent.config or {}).get("dreaming_state") or {}).get("run_id")
    return db.get(ChatMessage, ident) if ident else None


def run_state(row):
    return dict((row.extra_data or {}).get(KEY) or {}) if row is not None else {}


def save(row, data):
    row.extra_data = {**(row.extra_data or {}), KEY: data}


def pending_query(agent):
    """Journal receipts written to the journal and not yet dreamed, oldest first."""
    receipt = ChatMessage.extra_data[RECEIPT]
    return select(ChatMessage).where(
        receipt["agent_id"].as_string() == agent.id,
        receipt["status"].as_string() == "written",
        receipt["dreamed"].as_boolean().is_not(True),
    ).order_by(ChatMessage.created_at, ChatMessage.message_id)


def pending_count(db, agent, cap=1):
    return db.scalar(select(func.count()).select_from(pending_query(agent).limit(cap).subquery()))


def view(db, agent, now=None):
    if agent is None: return None
    now = time.time() if now is None else now
    row = current(db, agent)
    data = run_state(row)
    dreaming = (agent.config or {}).get("dreaming_state") or {}
    return {**options(agent.config), "timezone": user_zone(agent.config),
            "next_run": schedule(agent.config, now)[1],
            "run_id": row.message_id if row else None,
            **{k: data.get(k) for k in ("status", "started_at", "attempts", "next_retry", "error", "outcomes")},
            "completed_at": data.get("completed_at") or dreaming.get("last_completed_at"),
            "pending": pending_count(db, agent, cap=1000),
            "genesis": memory_state.genesis(agent).get("status")}


def create(db, agent, now, reason="scheduled"):
    ident = "pa_dream_" + uuid.uuid4().hex
    row = ChatMessage(message_id=ident, chat_id=agent.chat_id, role="system", content="Dreaming",
                      extra_data={KEY: {"status": "pending", "agent_id": agent.id, "attempts": 0, "next_retry": 0,
                                        "started_at": now, "reason": reason}})
    db.add(row)
    agent.config = {**agent.config, "dreaming_state": {**((agent.config or {}).get("dreaming_state") or {}),
                                                         "run_id": ident, "slot": schedule(agent.config, now)[0]}}
    return row


def run_now(db, user_id, now=None):
    now = time.time() if now is None else now
    agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == str(user_id)).with_for_update().execution_options(populate_existing=True))
    if agent is None: raise LookupError("Personal Agent not found")
    if agent.status != "ready": raise ValueError("Personal Agent is paused")
    row = current(db, agent)
    data = run_state(row)
    if data.get("status") in {"pending", "running"}: return view(db, agent, now)
    if data.get("status") in {"retry", "failed"}:
        data.update(status="pending", attempts=0, next_retry=0, error=None)
        save(row, data)
    else:
        if now - data.get("started_at", 0) < 60: raise ValueError("Wait a minute before the next Dreaming run")
        create(db, agent, now, "manual")
    db.commit()
    from personal_agent.memory.dreaming import wake
    wake.notify()
    return view(db, agent, now)


def _due(db, agent, data, now):
    """Why a new run should start now, or None. Only four things start one: the nightly slot,
    a run that left sources for a continuation, genesis, and the owner's "run now" (run_now)."""
    if data.get("continuation"): return "continuation"
    if memory_state.genesis(agent).get("status") in ("requested", "running"): return "genesis"
    slot, _ = schedule(agent.config, now)
    count = pending_count(db, agent)
    if ((agent.config or {}).get("dreaming_state") or {}).get("slot", 0) < slot:
        if count: return "scheduled"
        # Nothing to dream: the slot is spent without a run.
        agent.config = {**agent.config, "dreaming_state": {**(agent.config.get("dreaming_state") or {}), "slot": slot}}
        return None
    return None


def claim(sessions, ident, now):
    """Start or resume this agent's due run: (run_id, owner, attempt), or None."""
    with sessions() as db:
        agent = locked(db, ident)
        if agent is None or agent.status != "ready" or not access.allowed(db, agent.user_id): return None
        row = current(db, agent)
        data = run_state(row)
        # A failed run waits for the owner's "run now" or the next nightly slot.
        if data.get("status") == "failed" and schedule(agent.config, now)[0] <= data.get("started_at", 0): return None
        if not row or data.get("status") in ("done", "failed"):
            reason = _due(db, agent, data, now)
            if reason is None:
                db.commit()
                return None
            row = create(db, agent, now, reason)
            data = run_state(row)
        if data.get("next_retry", 0) > now: return None
        # A live lease belongs to a running attempt (ours elsewhere, or another writer's).
        if memory_state.busy(agent): return None
        if data.get("attempts", 0) >= MAX_ATTEMPTS:
            data.update(status="failed", error="Retry limit reached")
            save(row, data)
            db.commit()
            return None
        owner = "dream:" + uuid.uuid4().hex
        if not memory_state.hold(agent, owner): return None
        # Still "running" here means the last attempt died without abort(): dream() restores
        # its snapshot first (collect.recover).
        recover = data.get("status") == "running"
        data.update(status="running", owner=owner, attempts=data.get("attempts", 0) + 1, continuation=False,
                    recover=recover or bool(data.get("recover")))
        save(row, data)
        db.commit()
        return row.message_id, owner, data["attempts"]


def owned(db, ident, run_id, owner):
    """Lock the agent, check the run still owns its lease, renew it. Raises LeaseLost."""
    agent = locked(db, ident)
    row = current(db, agent) if agent else None
    if row is None or row.message_id != run_id: raise LeaseLost("Dreaming run replaced")
    data = run_state(row)
    lease = (agent.config or {}).get(memory_state.LEASE) or {}
    if data.get("owner") != owner or data.get("status") != "running" or lease.get("owner") != owner or lease.get("until", 0) <= time.time():
        raise LeaseLost("Dreaming lease lost")
    if agent.status != "ready" or not access.allowed(db, agent.user_id):
        raise LeaseLost("Personal Agent paused or access revoked")
    memory_state.hold(agent, owner)
    return agent, row, data


def renew(sessions, ident, run_id, owner):
    with sessions() as db:
        owned(db, ident, run_id, owner)
        db.commit()


def due_at(agent, row, data, now):
    """When this agent next needs the Dreaming loop: a timestamp (now or later), or None.

    Mirrors claim(): a pending or retrying run waits for its retry time and for a live lease
    to expire (a crashed attempt is recovered then); a failed run waits for the next slot;
    otherwise the next nightly slot, or now when this slot is still unspent."""
    if agent.status != "ready": return None
    status = data.get("status")
    if row is not None and status in ("pending", "running", "retry"):
        lease = (agent.config or {}).get(memory_state.LEASE) or {}
        return max(now, data.get("next_retry", 0) or 0, lease.get("until", 0) if memory_state.busy(agent) else 0)
    if status == "failed": return max(now, schedule(agent.config, data.get("started_at", 0))[1])
    if data.get("continuation") or memory_state.genesis(agent).get("status") in ("requested", "running"): return now
    slot, following = schedule(agent.config, now)
    return now if ((agent.config or {}).get("dreaming_state") or {}).get("slot", 0) < slot else following


def plan(sessions, now):
    """(agents due now, the earliest later time any agent is due or None)."""
    due, later = [], None
    with sessions() as db:
        for agent in db.scalars(select(PersonalAgent).where(PersonalAgent.status == "ready")):
            row = current(db, agent)
            at = due_at(agent, row, run_state(row), now)
            if at is None: continue
            if at <= now: due.append(agent.id)
            elif later is None or at < later: later = at
    return due, later

