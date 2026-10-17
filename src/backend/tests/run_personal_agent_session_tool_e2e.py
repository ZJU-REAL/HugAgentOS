"""Run inside a development backend: talk to a real Personal Agent through its message inbox,
as the page does, and watch how it uses the typed HugAgentOS session tool.

The agent runs on the configured model with the real worker; nothing is mocked. Never run
against production: the messages land in the owner's lifelong conversation.

  python tests/run_personal_agent_session_tool_e2e.py say <uid> "<message>"
  python tests/run_personal_agent_session_tool_e2e.py tasks <uid>
  python tests/run_personal_agent_session_tool_e2e.py approve <uid> <task_id> [once|task]
  python tests/run_personal_agent_session_tool_e2e.py wait-event <uid> <since_epoch> [seconds]
"""

import json
import sys
import time
import uuid
from sqlalchemy import select
from core.db.engine import SessionLocal
from core.db.models import ChatMessage
from personal_agent.conversation.front import reply_id
from personal_agent.conversation.message_routing import admit
from personal_agent.models import PersonalAgent, PersonalMessageInbox, PersonalWorkItem
from personal_agent.context import AgentContext


def show_reply(db, ident):
    row = db.get(ChatMessage, reply_id(ident))
    calls = [{"name": c.get("tool_name"), "args": c.get("tool_args"), "result": str(c.get("result"))[:300]} for c in (row.tool_calls or [])] if row else []
    return {"reply": row.content if row else None, "tool_calls": calls}


def say(uid, text, wait=240):
    with SessionLocal() as db:
        view = admit(AgentContext(db, uid), text, "e2e:" + uuid.uuid4().hex)
        ident = view["message_id"]
    end = time.time() + wait
    while time.time() < end:
        with SessionLocal() as db:
            inbox = db.get(PersonalMessageInbox, ident)
            if inbox.status in {"done", "failed"}:
                out = {"status": inbox.status, "tasks": (inbox.decision or {}).get("work_item_ids", []), **show_reply(db, ident)}
                print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
                return
        time.sleep(2)
    print(json.dumps({"status": "timeout", "id": ident}))


def tasks(uid):
    with SessionLocal() as db:
        agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == uid))
        rows = db.scalars(select(PersonalWorkItem).where(PersonalWorkItem.agent_id == agent.id)
            .order_by(PersonalWorkItem.created_at.desc()).limit(3)).all()
        print(json.dumps([{"id": t.id, "status": t.status, "goal": t.goal[:200], "waiting": t.waiting_reason,
            "pending": (t.checkpoint or {}).get("pending_approval"), "observed": (t.checkpoint or {}).get("observed_runs"),
            "result": (t.result or "")[:1500]} for t in rows], ensure_ascii=False, indent=1, default=str))


def approve(uid, task_id, scope="once"):
    from personal_agent.tasks.approvals import approve as decide
    with SessionLocal() as db:
        task = db.get(PersonalWorkItem, task_id)
        pending = (task.checkpoint or {})["pending_approval"]
        print(json.dumps(pending["action"], ensure_ascii=False))
        decide(AgentContext(db, uid), task_id, pending["id"], True, scope)
    print("approved")


def wait_event(uid, since, wait=600):
    """The agent's reply to the run watcher's event, once it arrives."""
    end = time.time() + wait
    while time.time() < end:
        with SessionLocal() as db:
            agent = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == uid))
            rows = db.scalars(select(PersonalMessageInbox).where(PersonalMessageInbox.agent_id == agent.id,
                PersonalMessageInbox.request_key.like("run:%"))).all()
            for inbox in rows:
                if inbox.created_at.timestamp() >= since and inbox.status in {"done", "failed"}:
                    event = db.get(ChatMessage, inbox.message_id)
                    print(json.dumps({"event": event.content, "status": inbox.status, **show_reply(db, inbox.message_id)},
                        ensure_ascii=False, indent=1, default=str))
                    return
        time.sleep(3)
    print(json.dumps({"status": "timeout"}))


if __name__ == "__main__":
    command, *rest = sys.argv[1:]
    {"say": lambda: say(rest[0], rest[1]), "tasks": lambda: tasks(rest[0]),
     "approve": lambda: approve(rest[0], rest[1], *(rest[2:3])),
     "wait-event": lambda: wait_event(rest[0], float(rest[1]), *(int(x) for x in rest[2:3]))}[command]()
