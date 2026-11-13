"""The macOS accessibility host: an embedded Cua Driver that serves ``read`` and ``steps``.

The driver runs inside this backend process, never as the CuaDriver.app daemon, so macOS
attributes Accessibility and Screen Recording to the desktop app that started the backend.
Only the steps of contract A.2 are translated into Cua Driver tools; no other Cua tool is
reachable from the cloud, so ``run_script``, ``kill_app``, ``set_config``, recording and
foreground delivery cannot be asked for. Every input is delivered in the background.

When the driver cannot serve this computer (another OS, the package missing, a runtime
that is not embedded, Accessibility not granted), ``available()`` is False with a reason
and the runner keeps the screenshot driver.
"""

import asyncio
import json
import os
import platform
import re
from typing import Optional

from core.infra.logging import get_logger
from personal_agent.device_link.driver import SKILLS, chord, element_rows, steps_refusal

logger = get_logger(__name__)

# What a read asks for: a compact markdown tree bounded to 250 nodes, and a JPEG of at most
# 1366 px only when the model wants to look (contract: elements mode reads carry no image
# unless asked).
READ_OPTIONS = {"tree_format": "markdown", "max_elements": 250, "timeout_ms": 1000,
                "screenshot_format": "jpeg", "screenshot_quality": 70, "max_image_dimension": 1366}
WINDOW = re.compile(r"^([0-9]{1,10}):([0-9]{1,10})$")
MAX_WINDOWS = 30
FRONT_TRIES = 3
MAX_QUERY = 200
EFFECTS = ("confirmed", "partial", "unverifiable", "suspected_noop", "refused")
# The embedded runtime holds a display-awake assertion after each tool call (600 s by
# default). A minute covers a working batch without keeping an idle Mac's display on.
KEEP_DISPLAY_AWAKE = "60"
# Our key names where Cua's differ. Cua's "delete" is Backspace; ours is forward delete.
CUA_KEYS = {"enter": "return", "delete": "forward_delete"}
OPEN = "/usr/bin/open"
# Element rows of the reads this host served, by snapshot, to re-find an approved element after
# a later read replaced its token (see driver.element_rows).
KEPT_SNAPSHOTS = 16



def _sdk():
    import cua_driver

    return cua_driver


def _window(value) -> Optional[tuple]:
    match = WINDOW.match(value) if isinstance(value, str) else None
    return (int(match[1]), int(match[2])) if match else None


def _key(name: str) -> str:
    return CUA_KEYS.get(name, name.lower())


def _named_key(name: str) -> tuple:
    """One of the driver's KEYS ("ctrl+c", "enter") as a Cua key and modifiers.

    As in the screenshot driver, ``ctrl+`` means Command on a Mac and ``alt`` is Option.
    """
    *modifiers, key = name.split("+")
    return _key(key), [{"ctrl": "cmd", "alt": "option"}.get(item, item) for item in modifiers]


def cua_call(step: dict, pid: int, window_id: int) -> tuple:
    """The one Cua Driver tool call for one validated step: ``(tool, arguments)``.

    Raises ValueError for a step this host cannot express; nothing is sent for it.
    """
    kind = step["type"]
    window = {"pid": pid, "window_id": window_id}
    target = {**window, "element_token": step["element"]} if "element" in step else \
        {**window, "x": step["x"], "y": step["y"]} if "x" in step else dict(window)
    background = {"delivery_mode": "background"}
    if kind in ("click", "double_click", "right_click"):
        return kind, {**target, **background}
    if kind == "move":
        if "x" not in step:
            raise ValueError("move needs x and y")
        return "move_cursor", {"x": step["x"], "y": step["y"]}
    if kind == "type":
        return "type_text", {**target, **background, "text": step["text"]}
    if kind == "set_value":
        return "set_value", {**target, "value": step["text"]}
    if kind == "key":
        key, modifiers = _named_key(step["key"])
        return "press_key", {**target, **background, "key": key, **({"modifiers": modifiers} if modifiers else {})}
    if kind == "hotkey":
        modifiers, key = chord(step["keys"])
        if not modifiers:
            return "press_key", {**target, **background, "key": _key(key)}
        return "hotkey", {**target, **background, "keys": [*modifiers, _key(key)]}
    if kind == "scroll":
        amount = step.get("amount", 3)
        direction = step.get("direction") or ("up" if amount >= 0 else "down")
        return "scroll", {**target, **background, "direction": direction, "amount": min(max(abs(amount), 1), 50)}
    if kind == "menu":
        return "invoke_menu", {**window, "path": step["path"]}
    raise ValueError("unsupported step")


def _structured(result) -> dict:
    try:
        data = json.loads(result.structured_json) if result.structured_json else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _message(result) -> str:
    return (result.text or "").strip().split("\n", 1)[0][:500]


def step_report(index: int, step: dict, result) -> dict:
    """Cua's own verdict on one step, passed through as it is."""
    data = _structured(result)
    ok = not result.is_error
    action = getattr(result, "action", None)
    effect = getattr(getattr(action, "effect", None), "name", "").lower() or data.get("effect")
    if effect not in EFFECTS:
        effect = "unverifiable" if ok else "refused"
    report = {"index": index, "type": step["type"], "ok": ok, "effect": effect, "message": _message(result)}
    hint = getattr(action, "hint", None) or data.get("hint")
    if hint:
        report["hint"] = hint
    if isinstance(data.get("next_call"), dict):
        report["next_call"] = data["next_call"]
    if not ok and result.error_code:
        report["code"] = result.error_code
    return report


def _window_view(record: dict) -> dict:
    return {"window": "%s:%s" % (record.get("pid"), record.get("window_id")), "app": record.get("app_name"),
            "title": record.get("title"), "on_screen": bool(record.get("is_on_screen"))}


class UiHost:
    """The process-wide embedded driver. Started on first use, stopped with the runner."""

    def __init__(self, load=_sdk, system=platform.system):
        self._load = load
        self._system = system
        self._sdk = None
        self._driver = None
        # Set when the driver can never run in this process; permissions are re-checked.
        self._failure: Optional[str] = None
        self._lock: Optional[asyncio.Lock] = None
        self.reason: Optional[str] = None
        self._rows: dict = {}        # snapshot_id -> element rows (see element_rows)
        self._latest: dict = {}      # (pid, window_id) -> latest snapshot_id read

    async def available(self) -> bool:
        if self._system() != "Darwin":
            self.reason = "not_macos"
            return False
        if self._driver is None and self._failure is None:
            self._lock = self._lock or asyncio.Lock()
            async with self._lock:
                if self._driver is None and self._failure is None:
                    await self._start()
        if self._failure:
            self.reason = self._failure
            return False
        try:
            granted = self._sdk.current_mac_os_permission_status()
        except Exception:
            logger.exception("[ui-host] permission check failed")
            self.reason = "permission_check_failed"
            return False
        # The accessibility tree and background input need Accessibility. Screen Recording
        # only adds screenshots; a read without one says so in degraded_reason.
        self.reason = None if granted.accessibility else "accessibility_permission_missing"
        return self.reason is None

    async def _start(self) -> None:
        # Never reach for a CuaDriver.app daemon, and never activate a window briefly.
        os.environ["CUA_DRIVER_EMBEDDED"] = "1"
        os.environ.pop("CUA_DRIVER_ALLOW_BRIEF_FOREGROUND", None)
        os.environ.setdefault("CUA_DRIVER_KEEP_DISPLAY_AWAKE", KEEP_DISPLAY_AWAKE)
        try:
            sdk = self._load()
        except ImportError:
            self._failure = "cua_driver_not_installed"
            return
        except Exception:
            logger.exception("[ui-host] Cua Driver failed to load")
            self._failure = "cua_driver_failed_to_load"
            return
        try:
            # Construction is synchronous; ``embedded=True`` skips the daemon search that
            # can block for 15 s.
            driver = await asyncio.to_thread(sdk.CuaDriver.create, None, embedded=True)
        except Exception:
            logger.exception("[ui-host] Cua Driver failed to start")
            self._failure = "cua_driver_failed_to_start"
            return
        if driver.execution_mode() != sdk.DriverExecutionMode.EMBEDDED:
            self._failure = "cua_driver_not_embedded"
            await self._shutdown(driver)
            return
        self._sdk, self._driver = sdk, driver
        logger.info("[ui-host] embedded Cua Driver %s started", getattr(sdk, "__version__", "?"))

    @staticmethod
    async def _shutdown(driver) -> None:
        try:
            await driver.shutdown()
        except Exception:
            logger.warning("[ui-host] Cua Driver shutdown failed", exc_info=True)

    async def close(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            await self._shutdown(driver)

    async def _call(self, tool: str, arguments: dict):
        return await self._driver.call_tool(tool, json.dumps(arguments))

    async def _windows(self) -> list:
        result = await self._call("list_windows", {})
        records = [record for record in _structured(result).get("windows") or []
                   if isinstance(record, dict) and record.get("layer", 0) == 0]
        # Front to back: on-screen first, then by stacking order (null: unknown, last).
        records.sort(key=lambda record: (not record.get("is_on_screen"), -(record.get("z_index") if isinstance(record.get("z_index"), int) else -1)))
        return records

    async def read(self, command: dict) -> dict:
        """Contract A.4 ``read``: the window list, plus one window's tree (and screenshot when asked)."""
        records = await self._windows()
        # Only windows on screen are listed with their titles: titles of the user's other windows
        # (chats, mail, private tabs) are not the agent's business. Apps in the background are named
        # without their windows; the user brings one forward when the task needs it.
        windows = [_window_view(record) for record in records if record.get("is_on_screen")][:MAX_WINDOWS]
        background = sorted({str(record.get("app_name")) for record in records
                             if not record.get("is_on_screen") and record.get("app_name")})[:MAX_WINDOWS]
        base = {"mode": "elements", "platform": "macos", "skill": SKILLS["elements"], "windows": windows,
                "background_apps": background,
                "truncated": False, "invalidated_snapshot_ids": [], "degraded_reason": None}
        if command.get("window") is None:
            # The front window, or the next one when it cannot be read (an overlay of a remote-desktop
            # or recording tool is often frontmost and has no tree).
            answer = None
            for record in [record for record in records if record.get("is_on_screen")][:FRONT_TRIES]:
                answer = await self._window_state((int(record["pid"]), int(record["window_id"])), command, base)
                if "error" not in answer:
                    return answer
            return answer or {**base, "window": None, "app": None, "title": None, "snapshot_id": None,
                              "elements": "", "degraded_reason": "no_window"}
        else:
            target = _window(command["window"])
            if target is None:
                return {"error": "invalid_window: use a window from the read's windows list", "not_executed": True,
                        "windows": windows}
        return await self._window_state(target, command, base)

    async def _window_state(self, target: tuple, command: dict, base: dict) -> dict:
        pid, window_id = target
        screenshot = bool(command.get("screenshot"))
        arguments = {"pid": pid, "window_id": window_id, **READ_OPTIONS, "include_screenshot": screenshot}
        if isinstance(command.get("query"), str) and command["query"].strip():
            arguments["query"] = command["query"][:MAX_QUERY]
        if isinstance(command.get("since"), str) and command["since"].strip():
            arguments["since"] = command["since"][:64]
        result = await self._call("get_window_state", arguments)
        data = _structured(result)
        if result.is_error:
            return {"error": "%s: %s" % (result.error_code or "read_failed", _message(result)),
                    "windows": base["windows"]}
        receipt = {
            **base,
            "window": "%d:%d" % target,
            "app": data.get("app_name"),
            "title": data.get("window_title"),
            "snapshot_id": data.get("snapshot_id"),
            # A ``since`` read returns only the changed rows.
            "elements": data.get("tree_diff") or data.get("tree_markdown") or "",
            "truncated": bool(data.get("truncated")),
            "invalidated_snapshot_ids": list(data.get("invalidated_snapshot_ids") or []),
            "degraded_reason": data.get("degraded_reason"),
        }
        if data.get("since_status"):
            receipt["since_status"] = data["since_status"]
        self._remember(target, receipt["snapshot_id"], data.get("tree_markdown") or "",
                       previous=self._latest.get(target) if data.get("tree_diff") else None)
        images = list(result.images or [])
        if images:
            receipt.update(image_base64=images[0].data_base64, media_type=images[0].mime_type,
                           width=data.get("screenshot_width"), height=data.get("screenshot_height"),
                           scale=data.get("screenshot_scale"))
        elif screenshot and not receipt["degraded_reason"]:
            error = data.get("screenshot_error")
            code = error.get("code") if isinstance(error, dict) else None
            receipt["degraded_reason"] = code or "screenshot_unavailable"
        return receipt

    def _remember(self, target: tuple, snapshot_id, markdown: str, previous=None) -> None:
        if not snapshot_id:
            return
        rows = element_rows(markdown)
        if previous and previous in self._rows:
            # A ``since`` read lists only the changed rows; the others keep their place.
            rows = {**self._rows[previous], **rows}
        self._rows[snapshot_id] = rows
        self._latest[target] = snapshot_id
        while len(self._rows) > KEPT_SNAPSHOTS:
            gone = next(iter(self._rows))
            self._rows.pop(gone)
            # A window whose latest read is gone has nothing to re-find against.
            for window in [window for window, latest in self._latest.items() if latest == gone]:
                self._latest.pop(window)

    async def _refind(self, batch: list, target: tuple) -> tuple:
        """Point steps planned on an older read of this window at the same elements in a fresh read.

        A later read replaces every token of the window, so a batch approved on an earlier one
        (a late approval, or a read in between) would be refused for stale tokens. An element is
        re-found only when exactly one row of the fresh read has its role, title and id; otherwise
        the step is refused and the agent reads again. Returns ``(batch, refusal or None)``.
        """
        stale = {step["element"].split(":", 1)[0] for step in batch if step.get("element")} - {self._latest.get(target)}
        if not stale:
            return batch, None
        if any(snapshot not in self._rows for snapshot in stale):
            return batch, None  # Not read by this process (it restarted): the driver judges the tokens.
        fresh = await self.read({"window": "%d:%d" % target, "screenshot": False})
        rows = self._rows.get(fresh.get("snapshot_id")) if "error" not in fresh else None
        if not rows:
            return batch, {"error": "stale_elements: read the window again", "not_executed": True}
        out = []
        for step in batch:
            element = step.get("element")
            if element and element.split(":", 1)[0] in stale:
                old = self._rows[element.split(":", 1)[0]].get(element.rsplit(":", 1)[1])
                matches = [index for index, row in rows.items() if old and row == old]
                if len(matches) != 1:
                    return batch, {"error": "stale_elements: the screen changed; read the window again",
                                   "not_executed": True, "after": fresh}
                step = {**step, "element": "%s:%s" % (fresh["snapshot_id"], matches[0])}
            out.append(step)
        return out, None

    async def steps(self, command: dict) -> dict:
        """Contract A.4 ``steps``: each step in order as one background Cua call, stopping at the
        first failure, then a ``since:"latest"`` read of the window as ``after``."""
        batch = command.get("steps")
        refusal = steps_refusal(batch, elements=True)
        if refusal:
            return refusal
        target = _window(command.get("window"))
        if target is None and any(step["type"] != "open_browser" for step in batch):
            return {"error": "invalid_window: steps need the window of the read they were planned on",
                    "not_executed": True}
        if target is not None:
            batch, refusal = await self._refind(batch, target)
            if refusal:
                return refusal
        reports, failed = [], None
        for index, step in enumerate(batch):
            report = await self._step(index, step, target)
            reports.append(report)
            if not report["ok"]:
                failed = index
                break
        result = {"ok": failed is None, "executed": len(batch) if failed is None else failed,
                  "failed_step": failed, "steps": reports}
        after = await self._after(target)
        if after is not None:
            result["after"] = after
        return result

    async def _step(self, index: int, step: dict, target: Optional[tuple]) -> dict:
        if step["type"] == "open_browser":
            return await self._open(index, step)
        try:
            tool, arguments = cua_call(step, *target)
        except ValueError as exc:
            return {"index": index, "type": step["type"], "ok": False, "effect": "refused", "message": str(exc)}
        try:
            result = await self._call(tool, arguments)
        except Exception as exc:
            # The call may have reached the app before it failed: its effect is unknown.
            logger.warning("[ui-host] %s failed: %s", tool, type(exc).__name__)
            return {"index": index, "type": step["type"], "ok": False, "effect": "unverifiable",
                    "message": "driver_error: %s" % type(exc).__name__}
        return step_report(index, step, result)

    @staticmethod
    async def _open(index: int, step: dict) -> dict:
        # ``open -g`` hands the address to the default browser without bringing it forward.
        process = await asyncio.create_subprocess_exec(
            OPEN, "-g", step["url"], stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            code = await asyncio.wait_for(process.wait(), 10)
        except asyncio.TimeoutError:
            process.kill()
            code = None
        ok = code == 0
        return {"index": index, "type": "open_browser", "ok": ok, "effect": "unverifiable" if ok else "refused",
                "message": "" if ok else "The default browser did not open the address"}

    async def _after(self, target: Optional[tuple]) -> Optional[dict]:
        """The window after the batch; the front window when that one is gone."""
        try:
            if target is not None:
                after = await self.read({"window": "%d:%d" % target, "since": "latest"})
                if "error" not in after:
                    return after
            return await self.read({})
        except Exception:
            logger.warning("[ui-host] read after steps failed", exc_info=True)
            return None


host = UiHost()
