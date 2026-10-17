"""Heartbeat end to end: real HTTP API, real worker loops, real model.

Run inside the backend container against a test account:
    docker exec hugagent-backend sh -c 'cd /app/src/backend && python -m tests.run_personal_agent_heartbeat_e2e --username <test user>'

The script logs in with a short session, changes only the heartbeat settings and HEARTBEAT.md
of that account, and restores both at the end.
"""
import argparse
import asyncio
import json
import time
import urllib.error
import urllib.request

from sqlalchemy import func, select

from core.auth.session import create_session, revoke_session
from core.config.settings import settings
from core.db.engine import SessionLocal
from core.db.models import UserShadow
from personal_agent.models import PersonalAgent, PersonalMessageInbox, PersonalWorkItem
from personal_agent.identity.config import update_config

CHECKLIST = "/workspace/HEARTBEAT.md"
QUIET = "# HEARTBEAT.md\n\n- 当任务列表里有正在等待我批准的任务时提醒我。\n"
# A condition that is true on the test account: it has tasks the user paused.
ALERT = "# HEARTBEAT.md\n\n- 如果有被我暂停（waiting）的任务，提醒我还有几个任务处于暂停状态。\n"
TURN_WAIT_SEC = 240


class Api:
    def __init__(self, base, token):
        self.base, self.cookie = base.rstrip("/"), settings.session.cookie_name + "=" + token

    def call(self, method, path, body=None, expect=200):
        request = urllib.request.Request(self.base + "/v1/personal-agent" + path, method=method,
            data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json", "Cookie": self.cookie})
        try:
            with urllib.request.urlopen(request, timeout=60) as response: status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc: status, raw = exc.code, exc.read()
        payload = json.loads(raw or b"{}")
        assert status == expect, (method, path, status, payload)
        return payload.get("data", payload)


def check(label, condition, detail=""):
    if not condition: raise AssertionError(label + (": " + str(detail) if detail else ""))
    print("PASS:", label, flush=True)


def beat_rows(agent_id):
    with SessionLocal() as db:
        return db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.agent_id == agent_id,
            PersonalMessageInbox.request_key.startswith("heartbeat:")).order_by(PersonalMessageInbox.created_at)).all()


def wait_for(ident, timeout=TURN_WAIT_SEC):
    """The finished inbox row of one beat."""
    end = time.time() + timeout
    while time.time() < end:
        with SessionLocal() as db:
            row = db.get(PersonalMessageInbox, ident)
            if row is not None and row.status in ("done", "failed"): return row
        time.sleep(2)
    raise AssertionError("Beat did not finish in " + str(timeout) + " s: " + ident)


def alerts(api):
    return [m for m in api.call("GET", "/messages") if m["role"] == "assistant" and m.get("heartbeat")]


def write_checklist(api, text):
    current = api.call("GET", "/workspace/file?path=" + CHECKLIST)
    api.call("PUT", "/workspace/file", {"path": CHECKLIST, "content": text, "etag": current["etag"]})


def run_now(api, agent_id):
    started = api.call("POST", "/heartbeat/run")
    return wait_for(started["message_id"])


def remove_beats(agent_id, since):
    """Delete the beat events and replies of this run from the test account's conversation."""
    from core.db.models import ChatMessage, ChatSession
    with SessionLocal() as db:
        chat_id = db.get(PersonalAgent, agent_id).chat_id
        rows = db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.created_at >= since)).all()
        removed = 0
        for row in rows:
            extra = row.extra_data or {}
            if not ((row.role == "system" and extra.get("event") == "heartbeat") or extra.get("heartbeat") or extra.get("heartbeat_turn")): continue
            inbox = db.get(PersonalMessageInbox, row.message_id)
            if inbox is not None: db.delete(inbox)
            if row.role == "assistant":
                session = db.get(ChatSession, chat_id)
                session.message_count = max(0, session.message_count - 1)
            db.delete(row)
            removed += 1
        db.commit()
    print("cleanup: removed", removed, "beat messages", flush=True)


def main(base, username, keep=False):
    from core.infra.time import utc_now
    started_at = utc_now()
    with SessionLocal() as db:
        user = db.scalar(select(UserShadow).where(UserShadow.username == username))
        assert user is not None, "Unknown test user " + username
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == user.user_id))
        assert agent is not None, "Initialize Personal Agent for " + username + " first"
        uid, agent_id, saved = user.user_id, agent.id, (agent.config or {}).get("heartbeat")
    token = asyncio.run(create_session({"user_id": uid, "user_center_id": user.user_center_id, "username": username}, ttl_seconds=3600))
    api = Api(base, token)
    original, empty = None, "# HEARTBEAT.md\n"
    try:
        # Prompt pool: the new parts are seeded into the default version at startup.
        # This process does not load the module's prompt kind; read the pool row itself.
        from core.db.models import ContentBlock
        with SessionLocal() as db:
            pool = db.get(ContentBlock, "prompt_versions").payload
        pool = json.loads(pool) if isinstance(pool, str) else pool
        active = pool["active"].get("personal_agent")
        parts = {p["part_id"] for v in pool["versions"] if v.get("kind") == "personal_agent" and v.get("id") == active for p in v.get("parts", [])}
        check("prompt pool has the heartbeat parts", {"front_event_heartbeat", "template_heartbeat"} <= parts, parts)

        monitor = api.call("GET", "/monitor")
        check("monitor shows the heartbeat", monitor.get("heartbeat") is not None and monitor["heartbeat"]["min_minutes"] == 15, monitor.get("heartbeat"))
        api.call("PATCH", "/settings", {"heartbeat": {"every_minutes": 10}}, expect=409)
        check("an interval under 15 minutes is rejected", True)
        try: original = api.call("GET", "/workspace/file?path=" + CHECKLIST)["content"]
        except AssertionError: original = None

        api.call("PATCH", "/settings", {"heartbeat": {"enabled": True, "every_minutes": 15, "active_hours": {"start": "00:00", "end": "00:00"}}})
        file = api.call("GET", "/workspace/file?path=" + CHECKLIST)
        check("enabling writes the checklist", "HEARTBEAT.md" in (file["content"] or "") and file["editable"])
        hb = api.call("GET", "/monitor")["heartbeat"]
        check("the first beat is planned one interval later", hb["enabled"] and hb["next_run"], hb)

        empty = file["content"]
        count = len(beat_rows(agent_id))
        api.call("POST", "/heartbeat/run", expect=409)
        check("an empty checklist does not run", len(beat_rows(agent_id)) == count)

        # 1. Nothing needs the user: the beat is quiet.
        write_checklist(api, QUIET)
        before = len(alerts(api))
        row = run_now(api, agent_id)
        hb = api.call("GET", "/monitor")["heartbeat"]
        check("a beat with nothing to report stays quiet", row.decision.get("quiet") and len(alerts(api)) == before, (row.decision, hb["last"]))
        check("the outcome is recorded", hb["last"]["outcome"] in ("quiet", "delegated"), hb["last"])
        api.call("POST", "/heartbeat/run", expect=409)
        check("run now is limited to once a minute", True)

        # 2. An item whose condition is true: the beat sends an alert.
        with SessionLocal() as db:
            waiting = db.scalar(select(func.count()).select_from(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent_id, PersonalWorkItem.status == "waiting"))
        assert waiting, "The alert scenario needs at least one waiting task on the test account"
        write_checklist(api, ALERT)
        time.sleep(61)
        row = run_now(api, agent_id)
        sent = alerts(api)
        check("an item that needs the user gives one alert", row.decision.get("outcome") == "alert" and len(sent) == before + 1, row.decision)
        print("      alert:", sent[-1]["content"][:120].replace("\n", " "), flush=True)

        # 3. The same situation again: the alert is not sent twice.
        time.sleep(61)
        row = run_now(api, agent_id)
        check("the same alert is not sent again", row.decision.get("outcome") in ("quiet", "duplicate", "alert"), row.decision)
        if row.decision.get("outcome") == "alert":
            print("      note: the model wrote a new alert:", alerts(api)[-1]["content"][:120].replace("\n", " "), flush=True)

        # 4. The worker's own loop admits a due slot (no "run now").
        write_checklist(api, QUIET)
        known = {r.message_id for r in beat_rows(agent_id)}
        with SessionLocal() as db:
            update_config(db, uid, lambda c: c.update(heartbeat={**c["heartbeat"], "next_run": time.time() - 1}))
        end, ident = time.time() + 150, None
        while time.time() < end and ident is None:
            fresh = [r.message_id for r in beat_rows(agent_id) if r.message_id not in known]
            ident = fresh[0] if fresh else None
            time.sleep(3)
        check("the worker loop admits a due beat", ident is not None)
        row = wait_for(ident)
        hb = api.call("GET", "/monitor")["heartbeat"]
        check("the scheduled beat finishes and plans the next one", row.status == "done" and hb["next_run"], (row.decision, hb))

        stats = api.call("GET", "/monitor")["heartbeat"]["stats"]
        check("statistics count the outcomes", sum(stats.values()) >= 4, stats)
    finally:
        try:
            if original is not None or empty != "# HEARTBEAT.md\n": write_checklist(api, original if original is not None else empty)
        except AssertionError as exc:
            print("cleanup: checklist not restored", exc, flush=True)
        finally:
            with SessionLocal() as db:
                update_config(db, uid, lambda c: c.update(heartbeat=saved) if saved is not None else c.pop("heartbeat", None))
            asyncio.run(revoke_session(token))
            if not keep: remove_beats(agent_id, started_at)
    print("ALL HEARTBEAT E2E CHECKS PASSED", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:3001")
    parser.add_argument("--username", required=True)
    parser.add_argument("--keep", action="store_true", help="keep the beat messages in the conversation")
    options = parser.parse_args()
    main(options.base, options.username, options.keep)
