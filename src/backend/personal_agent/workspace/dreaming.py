"""Durable, leased Personal Agent consolidation; isolated from ordinary chat memory."""
import personal_agent.workspace.files as _pa_files
import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from core.db.engine import SessionLocal
from core.db.models import ChatMessage
from personal_agent import access
from personal_agent.workspace import dream_files
from personal_agent.workspace import dream_plan
from personal_agent.workspace import file_memory;
from personal_agent.models import PersonalAgent
from personal_agent.scheduling.schedules import user_zone, zone

KEY = "personal_dreaming"
LEASE_SECONDS = 180
MAX_ATTEMPTS = 5

def options(config):
    return {"enabled": True, "hour": 3, **(config.get("dreaming") or {})}

def validate(patch, config):
    if not isinstance(patch, dict) or set(patch) - {"enabled", "hour"}: raise ValueError("Unknown Dreaming setting")
    settings = {**options(config), **patch}
    if type(settings["enabled"]) is not bool or type(settings["hour"]) is not int or not 0 <= settings["hour"] <= 23:
        raise ValueError("Invalid Dreaming setting")
    return settings

def schedule(config, now):
    local = datetime.fromtimestamp(now, zone(user_zone(config)))
    slot = local.replace(hour=options(config)["hour"], minute=0, second=0, microsecond=0)
    if slot > local: slot -= timedelta(days=1)
    return slot.timestamp(), (slot + timedelta(days=1)).timestamp()

def locked(db, ident):
    return db.scalar(select(PersonalAgent).where(PersonalAgent.id == ident).with_for_update().execution_options(populate_existing=True))

def current(db, agent):
    ident = (agent.config.get("dreaming_state") or {}).get("run_id")
    return db.get(ChatMessage, ident) if ident else None

def view(db, agent, now=None):
    if agent is None: return None
    now = time.time() if now is None else now
    row = current(db, agent)
    state = (row.extra_data or {}).get(KEY, {}) if row else {}
    return {**options(agent.config), "timezone": user_zone(agent.config),
        "next_run": schedule(agent.config, now)[1],
        "run_id": row.message_id if row else None,
        **{k: state.get(k) for k in ("status", "started_at", "attempts", "next_retry", "error", "outcomes")},
        "completed_at": state.get("completed_at") or (agent.config.get("dreaming_state") or {}).get("last_completed_at")}

def create(db, agent, now):
    ident = "pa_dream_" + uuid.uuid4().hex
    row = ChatMessage(message_id=ident, chat_id=agent.chat_id, role="system", content="Dreaming",
        extra_data={KEY: {"status": "pending", "agent_id": agent.id, "attempts": 0, "next_retry": 0, "started_at": now}})
    db.add(row)
    agent.config = {**agent.config, "dreaming_state": {**(agent.config.get("dreaming_state") or {}), "run_id": ident, "slot": schedule(agent.config, now)[0]}}
    return row

def run_now(db, user_id, now=None):
    now = time.time() if now is None else now
    agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == str(user_id)).with_for_update().execution_options(populate_existing=True))
    if agent is None: raise LookupError("Personal Agent not found")
    if agent.status != "ready": raise ValueError("Personal Agent is paused")
    if not options(agent.config)["enabled"]: raise ValueError("Enable Dreaming before running")
    row = current(db, agent)
    state = dict(row.extra_data[KEY]) if row else {}
    if state.get("status") in {"pending", "running"}: return view(db, agent, now)
    if state.get("status") in {"retry", "failed"}:
        state.update(status="pending", attempts=0, next_retry=0, error=None)
        row.extra_data = {KEY: state}
    else:
        if now - state.get("started_at", 0) < 60: raise ValueError("Wait a minute before the next Dreaming run")
        create(db, agent, now)
    db.commit()
    return view(db, agent, now)

def claim(sessions, ident, now):
    with sessions() as db:
        agent = locked(db, ident)
        if agent is None or agent.status != "ready" or not options(agent.config)["enabled"] or not access.allowed(db, agent.user_id): return
        row = current(db, agent)
        state = dict(row.extra_data[KEY]) if row else {}
        if state.get("status") == "failed": return
        if not row or state.get("status") == "done":
            slot, _ = schedule(agent.config, now)
            if (agent.config.get("dreaming_state") or {}).get("slot", 0) >= slot and not state.get("continuation"): return
            row = create(db, agent, now)
            state = dict(row.extra_data[KEY])
        if state.get("status") == "running" and state.get("lease_until", 0) > time.time(): return
        if state.get("next_retry", 0) > now: return
        if state.get("attempts", 0) >= MAX_ATTEMPTS:
            state.update(status="failed", error="Retry limit reached")
            row.extra_data = {KEY: state}
            db.commit()
            return
        token = uuid.uuid4().hex
        state.update(status="running", owner=token, lease_until=time.time() + LEASE_SECONDS, attempts=state.get("attempts", 0) + 1)
        row.extra_data = {KEY: state}
        db.commit()
        return row.message_id, token

def owned(db, ident, run_id, owner):
    agent = locked(db, ident)
    row = current(db, agent) if agent else None
    if row is None or row.message_id != run_id: raise RuntimeError("Dreaming run replaced")
    state = dict(row.extra_data[KEY])
    if state.get("owner") != owner or state.get("status") != "running" or state.get("lease_until", 0) <= time.time(): raise RuntimeError("Dreaming lease lost")
    if agent.status != "ready" or not options(agent.config)["enabled"] or not access.allowed(db, agent.user_id):
        raise RuntimeError("Dreaming disabled or access revoked")
    return agent, row, state

def snapshot(sessions, ident, run_id, owner):
    with sessions() as db:
        agent, row, state = owned(db, ident, run_id, owner)
        if state.get("plan"): return None, state.get("provider")
        with file_memory.workspace_lock(ident):
            skipped = list((state.get("payload") or {}).get("skipped", []))
            payload = {"skipped": skipped, "memory": {path: dream_files.read(ident, path) for path in dream_files.TARGETS},
                "sources": dream_files.collect(db, agent, skipped), "recovery": state.get("recovery", [])}
        state.update(payload=payload, provider=agent.config.get("model_provider_id"))
        row.extra_data = {KEY: state}
        db.commit()
        return payload, state["provider"]

def checkpoint(sessions, ident, run_id, owner, result, now):
    with sessions() as db:
        agent, row, state = owned(db, ident, run_id, owner)
        state["plan"] = dream_plan.plan(state["payload"], result, run_id, datetime.fromtimestamp(now, timezone.utc).isoformat())
        row.extra_data = {KEY: state}
        db.commit()

def apply(sessions, ident, run_id, owner, now):
    with sessions() as db:
        agent, row, state = owned(db, ident, run_id, owner)
        plan, payload = state["plan"], state["payload"]
        with file_memory.workspace_lock(ident):
            actual = {path: dream_files.read(ident, path) for path in dream_files.TARGETS}
            if any(actual[path] not in (plan["before"][path], plan["after"][path]) for path in actual):
                # Preserve partial successful writes. The next attempt replans against them.
                applied = [path for path in actual if actual[path] == plan["after"][path] and actual[path] != plan["before"][path]]
                if applied:
                    state["recovery"] = [*state.get("recovery", []), "Earlier partial writes preserved for manual review: " + ", ".join(applied)]
                state.pop("plan", None)
                row.extra_data = {KEY: state}
                db.commit()
                raise ValueError("Memory changed; re-evaluation required")
            evidence = {s["id"]: s for s in dream_files.collect(db, agent, mark_skipped=False)}
            if any(evidence.get(s["id"]) != s for s in payload["sources"]):
                applied = [path for path in actual if actual[path] == plan["after"][path] and actual[path] != plan["before"][path]]
                if applied:
                    state["recovery"] = [*state.get("recovery", []), "Earlier partial writes preserved for manual review: " + ", ".join(applied)]
                state.pop("plan", None)
                row.extra_data = {KEY: state}
                db.commit()
                raise ValueError("Daily evidence changed; re-evaluation required")
            for path in dream_files.TARGETS:
                if actual[path] != plan["after"][path]: _pa_files._write_file(ident, path, plan["after"][path])
            dream_files.append_report(ident, run_id, plan["report"])
            dream_files.acknowledge(db, payload["sources"])
            db.flush()  # Production sessions disable autoflush; query the updated receipts.
            state["continuation"] = dream_files.pending(db, agent)
        state.update(status="done", completed_at=now, error=None, next_retry=0, outcomes=plan["outcomes"])
        agent.config = {**agent.config, "dreaming_state": {**agent.config["dreaming_state"], "last_completed_at": now}}
        # Retain only compact audit metadata; evidence remains in daily files.
        state.pop("payload", None)
        state.pop("plan", None)
        row.extra_data = {KEY: state}
        db.commit()

def failed(sessions, ident, run_id, owner, exc, now):
    with sessions() as db:
        agent = locked(db, ident)
        row = current(db, agent) if agent else None
        if row is None or row.message_id != run_id: return
        state = dict(row.extra_data[KEY])
        if state.get("owner") != owner: return
        state.update(status="failed" if state["attempts"] >= MAX_ATTEMPTS else "retry",
            error=type(exc).__name__, next_retry=now + min(3600, 60 * 2 ** (state["attempts"] - 1)))
        row.extra_data = {KEY: state}
        db.commit()
        if not options(agent.config)["enabled"] or not access.allowed(db, agent.user_id): return
        try:
            with file_memory.workspace_lock(ident):
                dream_files.append_report(ident, run_id + "-attempt-" + str(state["attempts"]),
                    "## Dreaming attempt " + str(state["attempts"]) + "\n\nOutcome: " + state["status"]
                    + "; " + state["error"] + ". Retry status and next attempt are shown in settings.\n")
        except (OSError, RuntimeError, ValueError):
            pass  # Durable status remains visible even when the report volume is unavailable.

def ready_agents(sessions):
    with sessions() as db:
        return list(db.scalars(select(PersonalAgent.id).where(PersonalAgent.status == "ready")))

async def tick(sessions=None, now=None, complete=None):
    sessions = sessions or SessionLocal
    fixed_now = now
    complete = complete or dream_plan.complete
    for ident in await asyncio.to_thread(ready_agents, sessions):
        now = time.time() if fixed_now is None else fixed_now
        claimed = await asyncio.to_thread(claim, sessions, ident, now)
        if not claimed: continue
        run_id, owner = claimed
        try:
            payload, provider = await asyncio.to_thread(snapshot, sessions, ident, run_id, owner)
            if payload is not None:
                async with asyncio.timeout(75):
                    answer = await complete(payload, provider) if payload["sources"] else {"reflections": ["No new eligible daily records."], "operations": []}
                await asyncio.to_thread(checkpoint, sessions, ident, run_id, owner, answer, now)
            await asyncio.to_thread(apply, sessions, ident, run_id, owner, time.time() if fixed_now is None else now)
        except Exception as exc:
            await asyncio.to_thread(failed, sessions, ident, run_id, owner, exc, time.time() if fixed_now is None else now)
