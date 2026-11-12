"""Read-act tools for the agent's computers, with exclusive desktop control and exact approvals.

The agent sees two tools on every platform: pa_computer_read and pa_computer_act. Where
the screen is read as elements (a Mac with the elements driver) or as pixels (everything
else) is decided by the computer and reported in each read; the step vocabulary and the
approval are the same. An act answers with the screen it left behind (``after``), so the
model does not have to read again after every batch.
"""
import asyncio
import json
import shlex
import uuid
from datetime import timedelta

from agentscope.message import DataBlock, TextBlock
from agentscope.tool._response import ToolChunk
from opensandbox.models.execd import RunCommandOpts

from core.db.threads import run_db
from core.llm.factory.tools.definitions import function_tool
from personal_agent.computer import steps as step_rules
from personal_agent.computer.sandbox import DESKTOP_RUN, SANDBOX_PYTHON, get_personal_sandbox_provider
from personal_agent.device_link.driver import COMMAND_SECONDS, KEYS
from personal_agent.tasks.approvals import authorize, fingerprint, receipt
from personal_agent.tasks.guards import owned_task
from personal_agent.tools.responses import response

# One desktop, one user of it at a time: the agent and the owner share this lease.
LEASE_TTL = 45
# A batch can run longer than the lease; it is extended while the computer works.
LEASE_RENEW_SECONDS = 15
LOCK_WAIT_ATTEMPTS = 60
LOCK_POLL_SECONDS = 0.5
# What the cloud keeps of a read, so the model only copies the short snapshot_id. Long enough for a
# delayed approval to still be checked against the screen it was asked for.
SNAPSHOT_TTL = 3600
# Set when a busy desktop deferred this task, so the next call clears the durable mark only then.
WAIT_FLAG_TTL = 86400


def desktop_lease(target):
    return "pa:desktop:" + target


def _state():
    from core.infra.ephemeral import get_ephemeral_state
    return get_ephemeral_state()


def _check_target(target):
    if target != "cloud" and not (isinstance(target, str) and target.startswith("device:") and len(target) > 7):
        raise ValueError('target is "cloud" or "device:<id>" from pa_device_list')


# -- the cloud computer ---------------------------------------------------------------

async def _cloud_run(state, uid, action):
    """One driver invocation on the cloud computer. One session lookup per call; liveness is cached."""
    provider = get_personal_sandbox_provider()
    from personal_agent.computer.driver_install import ensure_driver
    session = await provider._get_or_create_session(state["box"], uid)
    # The backend installs the trusted driver. Never execute model-supplied code as the desktop user.
    driver = await ensure_driver(provider, state["box"], uid, session=session)
    command = SANDBOX_PYTHON + " " + driver + " " + shlex.quote(json.dumps(action))
    seconds = COMMAND_SECONDS.get(action.get("type"), 20)
    try:
        execution = await session.sandbox.commands.run(command, opts=RunCommandOpts(**DESKTOP_RUN, timeout=timedelta(seconds=seconds)))
    except Exception:
        getattr(provider, "forget_alive", lambda box: None)(state["box"])
        raise
    if execution.error:
        getattr(provider, "forget_alive", lambda box: None)(state["box"])
        detail = "".join(str(item.text) for item in execution.logs.stderr).strip().splitlines()
        raise RuntimeError("Cloud desktop action failed: " + (detail[-1][:300] if detail else "unknown error"))
    return json.loads("".join(str(item.text) for item in execution.logs.stdout))


async def cloud_driver(state, uid, action):
    """Run a command on the cloud computer. Its driver is installed from this backend, so it takes
    read and steps directly."""
    return await _cloud_run(state, uid, action)


# -- snapshots ------------------------------------------------------------------------

def _snapshot_key(ident, snapshot_id):
    return "pa:snapshot:" + ident + ":" + snapshot_id


async def remember(ident, result):
    """Keep a read's screen signature, window, app and mode in the backend, under its snapshot_id."""
    if not isinstance(result, dict): return result
    signature = result.pop("signature", None)
    if result.get("snapshot_id"):
        meta = {"signature": signature, "window": result.get("window"), "app": result.get("app"), "mode": result.get("mode")}
        if result.get("mode") == "elements":
            labels = step_rules.element_labels(result.get("elements"))
            if result.get("since_status"):
                # A ``since`` read lists only changed rows: keep the names of the rest from the read it follows.
                previous = await _state().get(_latest_key(ident, result.get("window")))
                if previous: labels = {**json.loads(previous), **labels}
            meta["labels"] = labels
            await _state().put(_latest_key(ident, result.get("window")), json.dumps(labels), ttl=SNAPSHOT_TTL)
        await _state().put(_snapshot_key(ident, str(result["snapshot_id"])), json.dumps(meta), ttl=SNAPSHOT_TTL)
    return result


def _latest_key(ident, window):
    return "pa:computer:labels:" + ident + ":" + str(window)


async def snapshot(ident, snapshot_id):
    """What the cloud kept of a read, or None when the snapshot is unknown or expired."""
    if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 128: return None
    value = await _state().get(_snapshot_key(ident, snapshot_id))
    if value: return json.loads(value)
    # A screen observed by the previous version of the tools, before this one was deployed.
    legacy = await _state().get("pa:observation:" + snapshot_id)
    return {"signature": legacy, "window": "screen", "app": None, "mode": "pixels"} if legacy else None


def _command(identity, meta):
    """The device command for an approved batch. Pixels screens carry the signature they were read with."""
    command = {"type": "steps", "window": identity.get("window") or "screen",
               "snapshot_id": identity.get("snapshot_id"), "steps": identity["steps"]}
    if (meta or {}).get("mode") != "elements" and (meta or {}).get("signature"):
        command["expected_signature"] = meta["signature"]
    return command


def without_images(result):
    if not isinstance(result, dict): return result
    clean = {key: value for key, value in result.items() if key != "image_base64"}
    if isinstance(clean.get("after"), dict): clean["after"] = without_images(clean["after"])
    return clean


# -- exclusive control ----------------------------------------------------------------

async def _renew(leases, key, token):
    while True:
        await asyncio.sleep(LEASE_RENEW_SECONDS)
        if not await leases.hold(key, token, ttl=LEASE_TTL): return


async def _dispatch(ident, uid, state, target, command):
    if target == "cloud": return await cloud_driver(state, uid, command)
    from personal_agent.computer.devices import execute_command
    return await execute_command(ident, uid, target.removeprefix("device:"), command)


async def operate(ident, uid, target, command, *, identity=None):
    """Run a command that changes the screen: under the desktop lease, after its approval."""
    from personal_agent.computer import computer_wait
    state = await run_db(owned_task, ident, uid)
    token, key = uuid.uuid4().hex, desktop_lease(state["agent_id"] if target == "cloud" else target)
    leases = _state()
    # Parallel calls from the same task, or a short user operation, release the lock quickly.
    held = await leases.hold(key, token, ttl=LEASE_TTL)
    for _ in range(LOCK_WAIT_ATTEMPTS):
        if held: break
        await asyncio.sleep(LOCK_POLL_SECONDS)
        await run_db(owned_task, ident, uid)
        held = await leases.hold(key, token, ttl=LEASE_TTL)
    if not held:
        await run_db(computer_wait.mark, ident, uid, key)
        await leases.put(computer_wait.flag(ident), key, ttl=WAIT_FLAG_TTL)
        return {"waiting": True, "automatic_retry": True,
                "message": "电脑暂时占用，释放后系统会自动继续。结束本轮，不要要求用户重试。"}
    renewal = asyncio.ensure_future(_renew(leases, key, token))
    try:
        await run_db(owned_task, ident, uid)
        if await leases.get(computer_wait.flag(ident)):
            await run_db(computer_wait.clear, ident, uid)
            await leases.drop(computer_wait.flag(ident))
        if identity is not None:
            digest, old = await run_db(authorize, ident, uid, identity)
            if old is not None: return old
        result = await _dispatch(ident, uid, state, target, command)
        if result.get("waiting") or result.get("outcome_unknown"):
            from personal_agent.tasks.approvals import wait_for_user
            await run_db(wait_for_user, ident, uid, result.get("message", "需要检查电脑状态后继续"))
        if identity is not None:
            await run_db(receipt, ident, uid, digest, without_images(result))
        return result
    finally:
        renewal.cancel()
        await leases.release(key, token)


# -- the two operations ---------------------------------------------------------------

async def read(ident, uid, target, window=None, query=None, since=None, screenshot=None):
    """Read a computer's screen. Read-only: no approval and no desktop lease, like the owner's preview."""
    _check_target(target)
    state = await run_db(owned_task, ident, uid)
    command = {"type": "read", "window": window, "query": query, "since": since, "screenshot": screenshot}
    result = step_rules.normalize_read(await _dispatch(ident, uid, state, target, command))
    if result.get("waiting"):
        from personal_agent.tasks.approvals import wait_for_user
        await run_db(wait_for_user, ident, uid, result.get("message", "需要检查电脑状态后继续"))
    return await remember(ident, result)


async def _settle_after(ident, result):
    if isinstance(result, dict) and isinstance(result.get("after"), dict):
        result["after"] = await remember(ident, step_rules.normalize_read(result["after"]))
    return result


async def act(ident, uid, target, steps, snapshot_id, window=None):
    """Request approval for one exact batch, run it, and answer with the screen it left."""
    _check_target(target)
    state = await run_db(owned_task, ident, uid)
    meta = await snapshot(ident, snapshot_id)
    if meta is None:
        return {"error": "unknown_snapshot", "not_executed": True,
                "message": "snapshot_id is unknown or expired. Call pa_computer_read and use its snapshot_id."}
    try:
        step_rules.validate_steps(steps, meta.get("mode"))
    except step_rules.NeedsElements:
        return {"error": "needs_elements_mode", "not_executed": True,
                "message": "This screen was read as pixels: use x and y, not element, set_value or menu."}
    identity = {"kind": "computer.steps", "target": target, "generation": state.get("generation"),
                "app": meta.get("app"), "window": window or meta.get("window") or "screen",
                "snapshot_id": snapshot_id, "steps": steps}
    targets = step_rules.targets_of(steps, meta.get("labels"))
    if targets:
        # What each element is, as the read named it: the approval card shows it instead of a row number.
        identity["targets"] = targets
    return await _settle_after(ident, await operate(ident, uid, target, _command(identity, meta), identity=identity))


async def replay(ident, uid, identity):
    """Run an approved action again before the model runs, exactly as it was approved.

    The approval is matched by the stored identity, never rebuilt: a snapshot cached at the time may
    have expired since. An old ``computer.action`` (checkpoints written before the batch tools) runs as
    a one-step batch under its own identity, so its approval still matches."""
    state = await run_db(owned_task, ident, uid)
    if identity.get("generation") != state.get("generation"):
        # The computer was replaced since the approval: the approved screen no longer exists.
        await run_db(receipt, ident, uid, fingerprint(identity), {"status": "not_executed",
            "message": "电脑已更换，批准的操作没有执行。请重新读取屏幕后再请求。"})
        return None
    if identity["kind"] == "computer.action":
        action = identity["action"]
        step = step_rules.validate_step(step_rules.step_from_legacy(action))
        sid = action.get("observation_id")
        meta = await snapshot(ident, sid) if sid else None
        command = _command({"window": "screen", "snapshot_id": sid, "steps": [step]}, meta or {"mode": "pixels"})
    else:
        meta = await snapshot(ident, identity.get("snapshot_id"))
        # Expired metadata: the computer itself still refuses tokens or a screen that no longer match.
        step_rules.validate_steps(identity["steps"], (meta or {}).get("mode") or "elements")
        command = _command(identity, meta)
    return await _settle_after(ident, await operate(ident, uid, identity["target"], command, identity=identity))


# -- tool results ---------------------------------------------------------------------

async def _with_image(ident, uid, scope, result, holder):
    """Answer with the screenshot attached for the model, and a private reference to deliver it."""
    from personal_agent.delivery.delivery_resources import stage
    data = holder.pop("image_base64", None) if isinstance(holder, dict) else None
    if not data: return response(result)
    media_type = holder.get("media_type") or "image/png"
    holder["resource_ref"] = await stage(ident, uid, scope, data)
    holder["resource_expires_in_seconds"] = 600
    return ToolChunk(content=[TextBlock(type="text", text=json.dumps(result, ensure_ascii=False)),
                              DataBlock(source={"type": "base64", "media_type": media_type, "data": data})])


def register_computer_tools(toolkit, ident, uid):
    async def pa_computer_read(target: str = "cloud", window: str | None = None, query: str | None = None,
                               since: str | None = None, screenshot: bool | None = None):
        """Read an authorized computer's screen. target is "cloud" or "device:<id>" from pa_device_list.
        Without window: the window list and the front window. window is "<pid>:<window_id>" from that
        list, or "screen". The answer's mode is "elements" (element tokens such as s0000002a:3, no image
        unless screenshot=true) or "pixels" (always a screenshot; act with x and y in its pixels).
        query filters elements and since="latest" returns only changes (elements mode only).
        Before acting, read /workspace/skills/<skill>/SKILL.md with pa_read_file for the skill named
        in the answer's "skill" field.
        The answer's snapshot_id is what pa_computer_act needs. A screenshot comes with a private
        resource_ref: give it to the user with pa_deliver(resource_ref=..., request_key=...); it
        expires after 10 minutes. Never try scrot, sudo, Xauthority or Bash to capture the desktop."""
        from personal_agent.delivery.delivery_resources import prepare
        _check_target(target)
        scope = await asyncio.to_thread(prepare, ident, uid, target)
        result = await read(ident, uid, target, window, query, since, screenshot)
        return await _with_image(ident, uid, scope, result, result)

    async def pa_computer_act(target: str, steps: list[dict], snapshot_id: str, window: str | None = None):
        """Request approval for one exact batch of 1 to 16 GUI steps, run it, and get the screen after it.
        snapshot_id comes from the latest pa_computer_read or the previous act's "after". window defaults
        to the read's window. Each step is {"type": ..., ...}; type is one of click, double_click,
        right_click, move, type, set_value, key, hotkey, scroll, open_browser, menu.
        Point with "element" (token from an elements read) or "x"/"y" (pixels of a pixels read).
        {"type": "click", "element": "s0000002a:3"}  {"type": "click", "x": 400, "y": 120}
        {"type": "type", "text": "hello"}  {"type": "set_value", "element": "...", "text": "..."}
        {"type": "key", "key": "enter"} (key is one of: {keys})  {"type": "hotkey", "keys": ["cmd", "s"]}
        {"type": "scroll", "direction": "down", "amount": 5}  {"type": "open_browser", "url": "https://..."}
        {"type": "menu", "path": ["File", "Save"]}. set_value, menu and element need an elements read.
        The batch stops at the first failed step. Check "after" and each step's "effect": unverifiable
        does not mean it worked. The batch needs user approval unless the user allowed this app for the
        task. To read web text, prefer the web fetch tool or curl in Bash."""
        from personal_agent.delivery.delivery_resources import prepare
        result = await act(ident, uid, target, steps, snapshot_id, window)
        after = result.get("after")
        if not (isinstance(after, dict) and after.get("image_base64")): return response(result)
        try:
            scope = await asyncio.to_thread(prepare, ident, uid, target)
        except PermissionError:
            # The batch ran; only the picture of its result may no longer be kept for delivery.
            after.pop("image_base64", None)
            return response(result)
        return await _with_image(ident, uid, scope, result, after)

    pa_computer_act.__doc__ = pa_computer_act.__doc__.replace("{keys}", ", ".join(sorted(KEYS)))

    async def pa_device_list():
        """List only this owner's paired computers and their online state. Your own cloud computer is never listed: use target "cloud"."""
        from personal_agent.computer.devices import list_devices
        return response(await run_db(list_devices, uid))

    for fn in (pa_computer_read, pa_computer_act, pa_device_list): toolkit.append(function_tool(fn))
