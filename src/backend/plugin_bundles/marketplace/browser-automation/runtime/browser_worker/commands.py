"""Browser operations are serialized and checked against the control epoch."""
import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from .protocol import Command, VIEWPORT_LIMITS

CONTROL_ACTIONS = {"take_control", "release_control", "heartbeat", "disconnect"}

def locator(page, params):
    if params.get("role"):
        return page.get_by_role(params["role"], name=params.get("name"), exact=True)
    selector = params.get("selector")
    if not isinstance(selector, str) or not selector or len(selector) > 4096:
        raise ValueError("selector_required")
    return page.locator(selector)

@asynccontextmanager
async def operation_lock(session, action):
    if action in CONTROL_ACTIONS or action == "dialog":
        yield
    else:
        async with session.lock:
            yield

async def execute(session, command: Command):
    if command.action == "heartbeat":
        session.last_active = time.monotonic()
        return {"ok": True}
    fingerprint = hashlib.sha256(json.dumps(command.model_dump(), sort_keys=True).encode()).hexdigest()
    if command.id in session.results:
        old, result = session.results[command.id]
        if old != fingerprint:
            raise ValueError("command_id_conflict")
        if result.get("error"):
            raise ValueError(result["error"])
        return result
    async with operation_lock(session, command.action):
        if command.id in session.results:
            old, result = session.results[command.id]
            if old != fingerprint:
                raise ValueError("command_id_conflict")
            if result.get("error"):
                raise ValueError(result["error"])
            return result
        if session.closed:
            raise ValueError("session_closed")
        session.last_active = time.monotonic()
        viewer_resize = command.action == "resize" and command.actor == "user"
        if viewer_resize:
            if not command.connection_id or (session.controller == "user" and command.connection_id != session.connection_id):
                raise ValueError("control_not_owned")
        elif command.action not in CONTROL_ACTIONS:
            if command.actor != session.controller:
                raise ValueError("control_not_owned")
            if command.actor == "user" and command.connection_id != session.connection_id:
                raise ValueError("control_not_owned")
            if command.epoch is not None and command.epoch != session.epoch:
                raise ValueError("stale_control_epoch")
            if session.private and command.actor == "agent":
                raise ValueError("private_observation")
        session.results[command.id] = (fingerprint, {"error": "operation_result_unknown"})
        result = await asyncio.wait_for(dispatch(session, command), 30)
        session.results[command.id] = (fingerprint, result)
        if len(session.results) > 200:
            session.results.pop(next(iter(session.results)))
        await session.publish_state()
        return result

async def dispatch(s, c):
    p, action = c.params, c.action
    navigation_wait = "commit" if c.actor == "user" else "domcontentloaded"
    if action == "state":
        return await s.state()
    if action == "take_control":
        if c.actor != "user" or not c.connection_id:
            raise ValueError("user_control_required")
        if s.controller == "user" and s.connection_id != c.connection_id:
            raise ValueError("control_already_owned")
        s.controller, s.connection_id = "user", c.connection_id
        s.private = bool(p.get("private", False))
        s.epoch += 1
        return await s.state()
    if action in {"release_control", "disconnect"}:
        if c.actor != "user" or c.connection_id != s.connection_id:
            raise ValueError("control_not_owned")
        s.controller = "agent" if action == "release_control" else "waiting"
        s.connection_id, s.private = "", False
        s.epoch += 1
        return await s.state()
    if action == "close":
        await s.close()
        return {"closed": True}
    if action == "new_tab":
        if len(s.pages) >= int(s.config.get("max_tabs", 12)):
            raise ValueError("tab_limit_reached")
        if p.get("url"):
            await s.policy.check(p["url"])
        page = await s.context.new_page()
        await s.add_page(page)
        tab = next(key for key, value in s.pages.items() if value == page)
        if p.get("url"):
            await page.goto(p["url"], wait_until=navigation_wait)
        return {"tab_id": tab}
    if action == "select_tab":
        await s.select(str(p["tab_id"]))
        return await s.state()
    page = s.pages.get(str(p.get("tab_id", s.active_tab)))
    if page is None:
        raise ValueError("unknown_tab")
    if action == "navigate":
        await s.policy.check(str(p["url"]))
        await page.goto(p["url"], wait_until=navigation_wait)
    elif action in {"back", "forward", "reload"}:
        await getattr(page, {"back": "go_back", "forward": "go_forward", "reload": "reload"}[action])(wait_until=navigation_wait)
    elif action == "close_tab":
        tab = next(key for key, value in s.pages.items() if value == page)
        await page.close()
        await s.page_closed(tab)
    elif action == "snapshot":
        return {"url": page.url, "title": await page.title(), "snapshot": (await page.locator("body").aria_snapshot())[:65536]}
    elif action == "text":
        return {"text": (await locator(page, p).inner_text())[:65536]}
    elif action == "screenshot":
        import base64
        return {"mime_type": "image/png", "data": base64.b64encode(await page.screenshot()).decode()}
    elif action == "click":
        await locator(page, p).click(timeout=10000)
    elif action == "fill":
        await locator(page, p).fill(str(p["text"]), timeout=10000)
    elif action == "select":
        await locator(page, p).select_option(p["value"], timeout=10000)
    elif action == "key":
        await page.keyboard.press(str(p["key"]))
    elif action == "input":
        if c.actor != "user" or int(p["viewport_revision"]) != s.viewport_revision:
            raise ValueError("stale_viewport")
        kind = p["kind"]
        if kind == "text":
            await page.keyboard.insert_text(str(p["text"])[:65536])
        elif kind in {"key_down", "key_up"}:
            await getattr(page.keyboard, "down" if kind == "key_down" else "up")(str(p["key"]))
        elif kind == "wheel":
            await page.mouse.move(float(p["x"]), float(p["y"]))
            await page.mouse.wheel(float(p["dx"]), float(p["dy"]))
        else:
            x, y = float(p["x"]), float(p["y"])
            await page.mouse.move(x, y)
            if kind in {"down", "up"}:
                await getattr(page.mouse, kind)(button=p.get("button", "left"))
            elif kind != "move":
                raise ValueError("unsupported_input")
    elif action == "resize":
        width, height = int(p["width"]), int(p["height"])
        if not (VIEWPORT_LIMITS["min_width"] <= width <= VIEWPORT_LIMITS["max_width"] and VIEWPORT_LIMITS["min_height"] <= height <= VIEWPORT_LIMITS["max_height"]):
            raise ValueError("invalid_viewport")
        if page.viewport_size == {"width": width, "height": height}:
            return {"ok": True, "url": page.url}
        await page.set_viewport_size({"width": width, "height": height})
        if page == s.pages.get(s.active_tab):
            await s.select(s.active_tab)
    elif action == "dialog":
        dialog = s.dialogs.pop(str(p.get("tab_id", s.active_tab)))
        if p.get("accept"):
            await dialog.accept(str(p.get("text", "")))
        else:
            await dialog.dismiss()
    elif action == "upload":
        import base64
        files = [{"name": f["name"], "mimeType": f.get("mime_type", "application/octet-stream"), "buffer": base64.b64decode(f["data"], validate=True)} for f in p["files"]]
        if sum(len(f["buffer"]) for f in files) > 8 * 1024 * 1024:
            raise ValueError("upload_too_large")
        chooser = s.choosers.pop(str(p.get("tab_id", s.active_tab)), None)
        if chooser:
            await chooser.set_files(files)
        else:
            await locator(page, p).set_input_files(files)
    elif action == "checkpoint":
        return {"state": await s.context.storage_state(indexed_db=True)}
    else:
        raise ValueError("unsupported_action")
    return {"ok": True, "url": page.url}
