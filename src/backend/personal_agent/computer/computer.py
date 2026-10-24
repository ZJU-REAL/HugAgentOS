"""Observe-act-verify tools with exclusive desktop control and exact approvals."""
from core.llm.factory.tools.definitions import function_tool
import asyncio
import json
import shlex
import uuid
from agentscope.message import DataBlock, TextBlock
from agentscope.tool._response import ToolChunk
from opensandbox.models.execd import RunCommandOpts
from datetime import timedelta
from personal_agent.tasks.guards import owned_task
from personal_agent.tools.responses import response
from personal_agent.computer.sandbox import DESKTOP_RUN, SANDBOX_PYTHON, get_personal_sandbox_provider
from personal_agent.tasks.approvals import authorize, receipt
from personal_agent.device_link.driver import ACTIONS as SUPPORTED, KEYS

# One desktop, one user of it at a time: the agent and the owner share this lease.
LEASE_TTL = 45
LOCK_WAIT_ATTEMPTS = 60
LOCK_POLL_SECONDS = 0.5

def desktop_lease(target):
    return "pa:desktop:" + target

def validate_action(action):
    if action.get("type") not in SUPPORTED:
        raise ValueError('Unsupported computer action. Use {"type": one of ' + ", ".join(sorted(SUPPORTED - {"observe"})) + ', ...}')
    allowed = {"type", "x", "y", "text", "key", "amount", "url", "observation_id"}
    if set(action) - allowed: raise ValueError("Unknown action parameters")
    for key in ("x", "y"):
        if key in action and (type(action[key]) is not int or not 0 <= action[key] <= 10000): raise ValueError("Invalid coordinate")
    if action.get("type") in {"click", "double_click", "right_click", "move"} and not {"x", "y"} <= set(action): raise ValueError("Coordinates are required")
    if "amount" in action and (type(action["amount"]) is not int or abs(action["amount"]) > 100): raise ValueError("Invalid scroll amount")
    if "text" in action and not isinstance(action["text"], str): raise ValueError("Text must be a string")
    if len(str(action.get("text", ""))) > 10000: raise ValueError("Text too long")
    if action.get("type") == "key" and action.get("key") not in KEYS: raise ValueError("Unsupported key")
    if action.get("type") == "open_browser":
        from urllib.parse import urlsplit
        parts = urlsplit(action.get("url", ""))
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username: raise ValueError("Invalid web URL")

async def cloud_driver(state, uid, action):
    provider = get_personal_sandbox_provider()
    from personal_agent.computer.driver_install import ensure_driver
    # The backend installs the trusted driver. Never execute model-supplied code as the desktop user.
    driver = await ensure_driver(provider, state["box"], uid)
    session = await provider._get_or_create_session(state["box"], uid)
    command = SANDBOX_PYTHON + " " + driver + " " + shlex.quote(json.dumps(action))
    execution = await session.sandbox.commands.run(command, opts=RunCommandOpts(**DESKTOP_RUN, timeout=timedelta(seconds=20)))
    if execution.error:
        detail = "".join(str(item.text) for item in execution.logs.stderr).strip().splitlines()
        raise RuntimeError("Cloud desktop action failed: " + (detail[-1][:300] if detail else "unknown error"))
    output = "".join(str(item.text) for item in execution.logs.stdout)
    return json.loads(output)

async def remember(result):
    """Keep the screen signature in the backend. The model copies only the short observation_id."""
    signature = result.pop("signature", None) if isinstance(result, dict) else None
    if signature and result.get("observation_id"):
        from core.infra.ephemeral import get_ephemeral_state
        await get_ephemeral_state().put("pa:observation:" + result["observation_id"], signature, ttl=600)
    return result

async def expect(action):
    """Return the action with the stored signature of the observation it refers to."""
    if action.get("type") == "observe" or not action.get("observation_id"): return action
    from core.infra.ephemeral import get_ephemeral_state
    signature = await get_ephemeral_state().get("pa:observation:" + str(action["observation_id"]))
    return {**action, "expected_signature": signature} if signature else action

async def operate(ident, uid, target, action, *, identity=None):
    state = owned_task(ident, uid)
    validate_action(action)
    from core.infra.ephemeral import get_ephemeral_state
    token, key = uuid.uuid4().hex, desktop_lease(state["agent_id"] if target == "cloud" else target)
    leases = get_ephemeral_state()
    # Parallel calls from the same task, or a short user operation, release the lock quickly.
    held = await leases.hold(key, token, ttl=LEASE_TTL)
    for _ in range(LOCK_WAIT_ATTEMPTS):
        if held: break
        await asyncio.sleep(LOCK_POLL_SECONDS)
        owned_task(ident, uid)
        held = await leases.hold(key, token, ttl=LEASE_TTL)
    if not held:
        from personal_agent.computer.computer_wait import mark
        mark(ident, uid, key)
        return {"waiting": True, "automatic_retry": True,
                "message": "电脑暂时占用，释放后系统会自动继续。结束本轮，不要要求用户重试。"}
    try:
        owned_task(ident, uid)
        from personal_agent.computer.computer_wait import clear
        clear(ident, uid)
        if identity is not None:
            digest, old = authorize(ident, uid, identity)
            if old is not None: return old
        if target == "cloud": result = await remember(await cloud_driver(state, uid, await expect(action)))
        else:
            from personal_agent.computer.devices import execute_command
            result = await remember(await execute_command(ident, uid, target.removeprefix("device:"), await expect(action)))
        if result.get("waiting") or result.get("outcome_unknown"):
            from personal_agent.tasks.approvals import wait_for_user
            wait_for_user(ident, uid, result.get("message", "需要检查电脑状态后继续"))
        if identity is not None:
            receipt(ident, uid, digest, {key: value for key, value in result.items() if key != "image_base64"})
        return result
    finally: await leases.release(key, token)

def register_computer_tools(toolkit, ident, uid):
    async def pa_computer_observe(target: str = "cloud"):
        """Observe an authorized desktop. Returns pixels, observation_id and a private resource_ref.
        To give this screenshot to the user, call pa_deliver(resource_ref=..., request_key=...).
        The latest four resources expire after 10 minutes. This tool does not publish files.
        Never try scrot, sudo, Xauthority or Bash to capture the protected desktop."""
        from personal_agent.delivery.delivery_resources import prepare, stage
        scope = await asyncio.to_thread(prepare, ident, uid, target)
        result = await operate(ident, uid, target, {"type": "observe"})
        data = result.pop("image_base64", None)
        if not data: return response(result)
        result["resource_ref"] = await stage(ident, uid, scope, data)
        result["resource_expires_in_seconds"] = 600
        return ToolChunk(content=[TextBlock(type="text", text=json.dumps(result)), DataBlock(source={"type": "base64", "media_type": "image/png", "data": data})])

    async def pa_computer_action(action: dict, target: str = "cloud"):
        """Request approval for one exact GUI action, then run it. Call pa_computer_observe first.
        action is a JSON object. Its "type" is one of: open_browser, click, double_click, right_click,
        move, type, key, scroll. Always copy "observation_id" from the latest observation.
        Examples: {"type": "open_browser", "url": "https://example.com", "observation_id": "..."}
        {"type": "click", "x": 400, "y": 120, "observation_id": "..."}  (pixels of the observation)
        {"type": "type", "text": "hello", "observation_id": "..."}
        {"type": "key", "key": "enter", "observation_id": "..."}  (key is one of: {keys})
        {"type": "scroll", "amount": -5, "x": 640, "y": 400, "observation_id": "..."}
        target is "cloud" or "device:<id>". Each action needs user approval unless the user allowed it for this task.
        To read web text, prefer the web fetch tool or curl in pa_bash; use the browser for pages that need a screen."""
        state = owned_task(ident, uid)
        validate_action(action)
        if action.get("type") == "observe": raise ValueError("Use pa_computer_observe")
        if not action.get("observation_id"): raise ValueError("Observe the target before acting")
        identity = {"kind": "computer.action", "target": target, "generation": state.get("generation"), "action": action}
        result = await operate(ident, uid, target, action, identity=identity)
        safe = {key: value for key, value in result.items() if key != "image_base64"}
        return response(safe)

    pa_computer_action.__doc__ = pa_computer_action.__doc__.replace("{keys}", ", ".join(sorted(KEYS)))

    async def pa_device_list():
        """List only this owner's paired computers and their online state. Your own cloud computer is never listed: use target "cloud"."""
        from personal_agent.computer.devices import list_devices
        return response(list_devices(uid))

    for fn in (pa_computer_observe, pa_computer_action, pa_device_list): toolkit.append(function_tool(fn))
