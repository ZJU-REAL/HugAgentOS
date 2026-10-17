"""Task-scoped exact-action approval; persisted outside the model's filesystem."""
import hashlib
import json
from core.db.engine import SessionLocal
from personal_agent.context import AgentContext

def fingerprint(action):
    return hashlib.sha256(json.dumps(action, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def intent(action):
    """The action without per-attempt fields. A rejection applies to every retry of the same action."""
    core = {key: value for key, value in action.items() if key != "request_key"}
    if isinstance(core.get("action"), dict):
        core["action"] = {key: value for key, value in core["action"].items() if key != "observation_id"}
    return core

def scope_key(action):
    """The class of actions a user can allow for one task. Email is never task-scoped."""
    if action.get("kind") == "computer.action":
        return "computer:" + str(action.get("target"))
    if action.get("kind") == "hugagent.session":
        return "session:" + str(action.get("target")) + ":" + str(action.get("operation"))
    return None

APPROVAL_MODES = ("ask", "auto", "full")
# Under "auto" these still ask: they reach other people or cannot be undone.
ALWAYS_ASK = {("email.send", None), ("hugagent.session", "chat.delete")}

def preapproved(mode, action):
    """True when the agent's permission level lets this action run without asking."""
    if mode == "full": return True
    return mode == "auto" and (action.get("kind"), action.get("operation")) not in ALWAYS_ASK

def authorize(task_id, uid, action):
    from personal_agent.tasks.guards import owned_task
    owned_task(task_id, uid)
    digest = fingerprint(action)
    with SessionLocal() as db:
        service = AgentContext(db, uid)
        task = service.tasks.get(task_id, lock=True)
        owned_task(task_id, uid)
        mode = (getattr(service.identity.agent(), "config", None) or {}).get("approval_mode") or "ask"
        state = dict(task.checkpoint or {})
        receipts = dict(state.get("action_receipts", {}))
        if digest in receipts and not receipts[digest].get("waiting"): return digest, receipts[digest]
        if intent(action) in [item["action"] for item in state.get("rejected_actions", [])]:
            return digest, {"status": "rejected", "message": "用户已拒绝此操作。不要再次请求。请改用其他方法，或说明无法完成。"}
        pending = state.get("pending_approval")
        if pending and pending.get("id") != digest:
            return digest, {"pending_approval": True, "message": "先处理已存在的操作批准"}
        # The user explicitly allowed this action class for this task only.
        scoped = scope_key(action) in state.get("approved_scopes", [])
        if digest not in state.get("approved_actions", []) and not scoped and not preapproved(mode, action):
            state["pending_approval"] = {"id": digest, "action": action}
            task.checkpoint = state
            db.commit()
            return digest, {"pending_approval": True, "message": "等待你批准操作"}
        # Once dispatched, an uncertain result must never be blindly repeated.
        receipts[digest] = {"status": "uncertain", "message": "操作已派发，尚未确认结果。请先核对。"}
        state["action_receipts"] = receipts
        state.pop("pending_approval", None)
        task.checkpoint = state
        db.commit()
        return digest, None

def receipt(task_id, uid, digest, result):
    with SessionLocal() as db:
        task = AgentContext(db, uid).tasks.get(task_id, lock=True)
        state = dict(task.checkpoint or {})
        results = dict(state.get("action_receipts", {}))
        results[digest] = result
        payloads = dict(state.get("approved_action_payloads", {}))
        payloads.pop(digest, None)
        state["approved_action_payloads"] = payloads
        state["action_receipts"] = results
        task.checkpoint = state
        db.commit()

def approve(service, ident, approval_id, accept, scope="once"):
    task = service.tasks.get(ident, lock=True)
    state = dict(task.checkpoint or {})
    pending = state.get("pending_approval") or {}
    # The run that asked can still be writing its reply. The decision counts at once;
    # the worker requeues the task when that run ends.
    if task.status not in {"waiting", "running"} or pending.get("id") != approval_id:
        raise ValueError("Approval no longer matches the waiting task")
    running = task.status == "running"
    if scope not in {"once", "task"}: raise ValueError("Unknown approval scope")
    state.pop("pending_approval", None)
    if accept:
        state["approved_actions"] = [*state.get("approved_actions", []), approval_id]
        state["approved_action_payloads"] = {**state.get("approved_action_payloads", {}), approval_id: pending["action"]}
        key = scope_key(pending["action"])
        if scope == "task":
            if key is None: raise ValueError("This action needs approval each time")
            state["approved_scopes"] = list(dict.fromkeys([*state.get("approved_scopes", []), key]))
    else:
        # The model reads the rejection and chooses another way. It must not repeat the action.
        receipts = dict(state.get("action_receipts", {}))
        receipts[approval_id] = {"status": "rejected", "message": "用户拒绝了此操作。不要重复此操作。请改用其他方法，或说明无法完成。"}
        state["action_receipts"] = receipts
        # Readable for the next model run, and matched for retries with a new request key.
        state["rejected_actions"] = [*state.get("rejected_actions", []), {"action": intent(pending["action"])}][-20:]
    if running: state["decided_during_run"] = True
    else: task.requeue()
    task.checkpoint = state
    service.db.commit()
    from personal_agent.tasks.views import task_view
    return task_view(task)

def wait_for_user(task_id, uid, reason):
    from personal_agent.tasks.guards import owned_task
    owned_task(task_id, uid)
    with SessionLocal() as db:
        task = AgentContext(db, uid).tasks.get(task_id, lock=True)
        owned_task(task_id, uid)
        task.checkpoint = {**(task.checkpoint or {}), "task_wait_reason": reason}
        db.commit()

def reconcile(service, ident, action_id, outcome):
    task = service.tasks.get(ident, lock=True)
    state = dict(task.checkpoint or {})
    results = dict(state.get("action_receipts", {}))
    old = results.get(action_id) or {}
    if task.status != "waiting" or not (old.get("status") == "uncertain" or old.get("outcome_unknown")):
        raise ValueError("No matching uncertain action")
    if outcome == "executed":
        results[action_id] = {"status": "confirmed_by_user", "message": "User checked that this exact action took effect. Do not repeat it."}
    elif outcome == "not_executed":
        results.pop(action_id)
        state["approved_actions"] = [item for item in state.get("approved_actions", []) if item != action_id]
    else:
        raise ValueError("Check the action before continuing")
    state["action_receipts"] = results
    task.checkpoint = state
    service.db.commit()
    from personal_agent.tasks.views import task_view
    return task_view(task)
