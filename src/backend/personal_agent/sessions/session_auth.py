"""Short task grants authorize only the session broker, never ordinary APIs."""

import hashlib
import json
import secrets
import time
from fastapi import HTTPException
from core.infra.ephemeral import get_ephemeral_state
from core.auth.backend import UserContext, get_current_user, security
from core.db.models import UserShadow
from personal_agent.models import PersonalWorkItem, PersonalAgent

def key(token):
    return "pa:session-grant:" + hashlib.sha256(token.encode()).hexdigest()

def _grant_payload(db, task_id, uid):
    from personal_agent.tasks.guards import owned_task

    owned_task(task_id, uid)
    task = db.get(PersonalWorkItem, task_id)
    return {
        "uid": uid,
        "task": task_id,
        "run": task.run_id,
        "owner": task.owner,
    }


async def mint(task_id, uid, ttl):
    from core.db.threads import run_in_session

    payload = await run_in_session(_grant_payload, task_id, uid)
    token = "pa_cli_" + secrets.token_urlsafe(32)
    await get_ephemeral_state().put(key(token), json.dumps(payload), ttl=ttl)
    return token

async def resolve(request):
    """The caller's identity and, for a task's CLI token, its grant."""
    from core.db.threads import run_in_session

    credentials = await security(request)
    token = credentials.credentials if credentials else ""
    if not token.startswith("pa_cli_"):
        user = await get_current_user(request, credentials)
        if user.api_key_agent_id:
            raise HTTPException(403, "Agent-specific key cannot manage sessions")
        return user, None
    raw = await get_ephemeral_state().get(key(token))
    if not raw:
        raise HTTPException(401, "Task grant expired")
    payload = json.loads(raw)
    return await run_in_session(_grant_owner, payload), payload


def _grant_owner(db, payload):
    """The task owner, provided the grant still matches the live task and its lease."""
    task = db.get(PersonalWorkItem, payload["task"])
    agent = db.get(PersonalAgent, task.agent_id) if task else None
    if (
        not task
        or not agent
        or agent.user_id != payload["uid"]
        or agent.status != "ready"
        or task.status != "running"
        or not task.lease_until
        or task.lease_until < time.time()
        or (task.run_id, task.owner)
        != (payload["run"], payload["owner"])
    ):
        raise HTTPException(401, "Task grant revoked")
    row = db.get(UserShadow, payload["uid"])
    if not row:
        raise HTTPException(401, "Owner unavailable")
    return UserContext(
        user_id=row.user_id, user_center_id=row.user_center_id or "", username=row.username
    )
