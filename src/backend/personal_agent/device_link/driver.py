"""Small trusted screenshot/input driver. JSON request in argv, JSON receipt on stdout.

Runs on the Personal Agent cloud computer and, through the desktop device link, on the
user's own computer. ``probe`` and ``request_permissions`` are local requests only; the
device link never accepts them from the cloud.
"""
import base64
import ctypes
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

# The complete action vocabulary. The cloud validates against it and pairs devices with it.
# ``read`` and ``steps`` are the batch commands; the others are single actions older clouds send.
ACTIONS = frozenset({"observe", "click", "double_click", "right_click", "move", "type", "key", "scroll", "open_browser",
                     "read", "steps"})
KEYS = {"enter": "enter", "tab": "tab", "escape": "esc", "backspace": "backspace", "ctrl+a": "ctrl+a", "ctrl+c": "ctrl+c", "ctrl+v": "ctrl+v", "alt+left": "alt+left",
        "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end", "delete": "delete", "pageup": "pageup", "pagedown": "pagedown",
        "ctrl+l": "ctrl+l", "ctrl+t": "ctrl+t", "ctrl+w": "ctrl+w", "ctrl+z": "ctrl+z", "ctrl+x": "ctrl+x", "ctrl+r": "ctrl+r", "ctrl+f": "ctrl+f"}


# Key codes per platform. "ctrl+" combinations mean Command on a Mac, as users expect.
MAC_KEYS = {"enter": (36, 0), "tab": (48, 0), "escape": (53, 0), "backspace": (51, 0), "ctrl+a": (0, 1 << 20), "ctrl+c": (8, 1 << 20),
            "ctrl+v": (9, 1 << 20), "alt+left": (123, 1 << 19), "up": (126, 0), "down": (125, 0), "left": (123, 0), "right": (124, 0),
            "home": (115, 0), "end": (119, 0), "delete": (117, 0), "pageup": (116, 0), "pagedown": (121, 0), "ctrl+l": (37, 1 << 20),
            "ctrl+t": (17, 1 << 20), "ctrl+w": (13, 1 << 20), "ctrl+z": (6, 1 << 20), "ctrl+x": (7, 1 << 20), "ctrl+r": (15, 1 << 20), "ctrl+f": (3, 1 << 20)}
MAC_FLAGS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}
MAC_CHARS = dict(zip("asdfhgzxcv", range(10))) | dict(zip("bqweryt123465", range(11, 24))) | dict(zip("97", (25, 26))) | {
    "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46}
WINDOWS_KEYS = {"enter": [13], "tab": [9], "escape": [27], "backspace": [8], "ctrl+a": [17, 65], "ctrl+c": [17, 67], "ctrl+v": [17, 86],
                "alt+left": [18, 37], "up": [38], "down": [40], "left": [37], "right": [39], "home": [36], "end": [35], "delete": [46],
                "pageup": [33], "pagedown": [34], "ctrl+l": [17, 76], "ctrl+t": [17, 84], "ctrl+w": [17, 87], "ctrl+z": [17, 90],
                "ctrl+x": [17, 88], "ctrl+r": [17, 82], "ctrl+f": [17, 70]}
WINDOWS_MODIFIERS = {"ctrl": 17, "cmd": 17, "alt": 18, "shift": 16}


def screenshot():
    system = platform.system()
    if system == "Linux":
        from PIL import ImageGrab
        return ImageGrab.grab(xdisplay=os.environ.get("DISPLAY", ":0"))
    if system == "Darwin":
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "screen.png"
            subprocess.run(["/usr/sbin/screencapture", "-x", "-m", str(path)], check=True, capture_output=True)
            image = Image.open(path).copy()
            cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            class Point(ctypes.Structure): _fields_ = [("x",ctypes.c_double),("y",ctypes.c_double)]
            class Size(ctypes.Structure): _fields_ = [("width",ctypes.c_double),("height",ctypes.c_double)]
            class Rect(ctypes.Structure): _fields_ = [("origin",Point),("size",Size)]
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]; cg.CGDisplayBounds.restype = Rect
            bounds = cg.CGDisplayBounds(cg.CGMainDisplayID()).size
            # Screenshot coordinates and Quartz input coordinates must use the same units.
            return image.resize((int(bounds.width),int(bounds.height)))
    if system == "Windows":
        from PIL import ImageGrab
        ctypes.windll.user32.SetProcessDPIAware()
        return ImageGrab.grab()
    raise RuntimeError("Unsupported device platform")


def signature(image):
    """A coarse grayscale grid. Small changes (clock, cursor) touch few cells."""
    small = image.convert("L").resize((32, 20))
    return "".join("%x" % (value >> 4) for value in small.getdata())


# Contract A.2: the steps of a ``steps`` command. The cloud and both execution paths (this
# driver and the macOS accessibility host) check them the same way.
STEP_TYPES = frozenset({"click", "double_click", "right_click", "move", "type", "set_value", "key", "hotkey", "scroll", "open_browser", "menu"})
STEP_FIELDS = frozenset({"type", "element", "x", "y", "text", "key", "keys", "amount", "direction", "url", "path"})
MODIFIERS = frozenset({"cmd", "ctrl", "alt", "shift"})
ELEMENT_TOKEN = re.compile(r"^s[0-9a-f]{8}:[0-9]+$")
MAX_STEPS, NEEDS_ELEMENTS = 16, "needs_elements_mode"
# How long the cloud waits for, and the device may spend on, each batch command.
COMMAND_SECONDS = {"read": 15, "steps": 75}
SKILLS = {"elements": "pa-computer-macos", "pixels": "pa-computer-pixels"}
# One element row of an accessibility read, e.g. ``- [3] AXButton "Send" [id=send actions=[press]]``.
ELEMENT_ROW = re.compile(r"\[(\d+)\]\s+(AX\w+)(.*)$")
ROW_TITLE = re.compile(r'^\s*"([^"]{0,200})"')
ROW_AX_ID = re.compile(r"\bid=([^\]=]+?)(?:\s+\w+=|\])")


def element_rows(markdown):
    """``{row index: (role, title, ax id)}`` of a read's element rows. The value is left out: it
    changes as the user types (so it cannot identify an element) and can be private."""
    rows = {}
    for line in (markdown or "").splitlines():
        match = ELEMENT_ROW.search(line)
        if not match: continue
        index, role, rest = match.groups()
        title, ax_id = ROW_TITLE.match(rest), ROW_AX_ID.search(rest)
        rows[index] = (role, title.group(1) if title else "", ax_id.group(1).strip() if ax_id else "")
    return rows


def _integer(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _web_address(url):
    try: parts = urlsplit(url) if isinstance(url, str) and len(url) <= 4096 else None
    except ValueError: return False
    return bool(parts and parts.scheme in ("http", "https") and parts.hostname and not parts.username)


def _hotkey(keys):
    part = lambda key: isinstance(key, str) and (key in MODIFIERS or key in KEYS or (len(key) == 1 and key.isascii() and key.isalnum()))
    return isinstance(keys, list) and 1 <= len(keys) <= 4 and all(map(part, keys))


def chord(keys):
    """Modifiers and the one key they apply to; ValueError for a combination input cannot press."""
    others = [key for key in keys if key not in MODIFIERS]
    if len(others) != 1 or "+" in others[0]: raise ValueError("hotkey needs modifiers and exactly one other key")
    return [key for key in keys if key in MODIFIERS], others[0]


def step_problem(step, elements):
    """Why one step cannot run, or None. ``elements`` is True on the accessibility path."""
    if not isinstance(step, dict) or step.get("type") not in STEP_TYPES: return "unknown step type"
    if set(step) - STEP_FIELDS: return "unknown field " + sorted(set(step) - STEP_FIELDS)[0]
    kind, has = step["type"], step.__contains__
    if not elements and (kind in ("set_value", "menu") or has("element")): return NEEDS_ELEMENTS
    checks = (
        (has("element") and not (isinstance(step["element"], str) and ELEMENT_TOKEN.match(step["element"])), "element must be a token from the latest read"),
        (any(has(axis) and not _integer(step[axis], 0, 10000) for axis in "xy") or has("x") != has("y"), "x and y must both be integers from 0 to 10000"),
        (kind in ("click", "double_click", "right_click", "move") and not has("element") and not (has("x") and has("y")), kind + " needs element, or x and y"),
        (kind in ("type", "set_value") and not (isinstance(step.get("text"), str) and len(step["text"]) <= 10000), kind + " needs text of at most 10000 characters"),
        (kind == "key" and step.get("key") not in KEYS, "key must be one of " + ", ".join(sorted(KEYS))),
        (kind == "hotkey" and not _hotkey(step.get("keys")), "keys must be 1 to 4 of cmd, ctrl, alt, shift, a letter or digit, or a named key"),
        (kind == "scroll" and has("amount") and not _integer(step["amount"], -100, 100), "amount must be an integer from -100 to 100"),
        (kind == "scroll" and has("direction") and step["direction"] not in ("up", "down", "left", "right"), "direction must be up, down, left or right"),
        (kind == "open_browser" and not _web_address(step.get("url")), "url must be an http or https address"),
        (kind == "menu" and not (isinstance(step.get("path"), list) and 1 <= len(step["path"]) <= 16
                                 and all(isinstance(label, str) and 0 < len(label) <= 200 for label in step["path"])), "path must hold 1 to 16 menu labels"),
    )
    return next((message for failed, message in checks if failed), None)


def steps_refusal(steps, elements):
    """The receipt for a batch that must not run, or None when every step is valid."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return {"error": "invalid_steps: a batch holds 1 to %d steps" % MAX_STEPS, "not_executed": True}
    problems = [(index, step_problem(step, elements)) for index, step in enumerate(steps)]
    if any(problem == NEEDS_ELEMENTS for _, problem in problems):
        return {"error": NEEDS_ELEMENTS, "not_executed": True}
    return next(({"error": "invalid_steps: step %d: %s" % (index, problem), "not_executed": True}
                 for index, problem in problems if problem), None)


def same_screen(action, before):
    """True when the screen still matches the observation that the action refers to.
    The backend adds expected_signature. Without it, only an exact match is accepted."""
    if action.get("observation_id") == before["observation_id"]: return True
    old, new = str(action.get("expected_signature") or ""), before["signature"]
    if len(old) != len(new) or not old: return False
    changed = sum(abs(int(a, 16) - int(b, 16)) > 1 for a, b in zip(old, new))
    return changed <= len(old) * 0.02


# PNG bytes one observation may take. Base64 adds a third, which keeps the receipt under
# the cloud's 8 MB limit and the image under what vision models accept (about 5 MB).
IMAGE_BUDGET = 3_500_000


def encode(image):
    """PNG of the screen within IMAGE_BUDGET, and the screen pixels per image pixel.

    A large or busy screen is first reduced to 256 colours (UI text stays sharp), then
    scaled down until it fits. Actions give coordinates in image pixels; execute()
    multiplies them by the scale to reach the screen.
    """
    def png(picture):
        data = io.BytesIO()
        picture.save(data, format="PNG", optimize=True)
        return data.getvalue()

    pixels = png(image)
    if len(pixels) <= IMAGE_BUDGET:
        return image, pixels, 1.0
    image = image.convert("RGB").quantize(colors=256)
    pixels = png(image)
    scale = 1.0
    while len(pixels) > IMAGE_BUDGET and image.width > 640:
        scale /= 0.75
        image = image.resize((int(image.width * 0.75), int(image.height * 0.75)))
        pixels = png(image)
    return image, pixels, scale


# After an input the screen is read once it stops changing: two equal fingerprints in a
# row, sampled every 50 ms, for at most 500 ms. A fixed pause was too short for some apps
# and wasted time for most.
SETTLE_INTERVAL = 0.05
SETTLE_LIMIT = 0.5


def settle():
    """The screen as it is once it stopped changing, or after SETTLE_LIMIT."""
    deadline = time.monotonic() + SETTLE_LIMIT
    image = screenshot(); last = signature(image)
    while time.monotonic() < deadline:
        time.sleep(SETTLE_INTERVAL)
        image = screenshot(); current = signature(image)
        if current == last: break
        last = current
    return image


def observe(image=None):
    image, pixels, scale = encode(screenshot() if image is None else image)
    return {"image_base64": base64.b64encode(pixels).decode(), "width": image.width, "height": image.height,
            "scale": scale, "observation_id": hashlib.sha256(pixels).hexdigest()[:32],
            "signature": signature(image.convert("RGB")), "platform": platform.system()}


def native_action(action):
    kind = action["type"]
    system = platform.system()
    if kind == "open_browser":
        import webbrowser
        if system == "Linux":
            browsers = sorted(Path("/ms-playwright").glob("chromium-*/chrome-linux*/chrome"))
            if not browsers:
                if not webbrowser.open(action["url"]): raise RuntimeError("Browser unavailable")
                return
            command = [str(browsers[0]), "--user-data-dir=/home/pa-desktop/browser", "--no-first-run", "--no-default-browser-check"]
            if Path("/usr/local/bin/pa-browser").is_file():
                # The cloud image's launcher: same profile and flags as the desktop menu.
                command = ["/usr/local/bin/pa-browser"]
            elif Path("/opt/personal-agent").is_dir():
                # The hardened cloud container has no setuid or user-namespace sandbox.
                # The container is the isolation boundary. Never use this on a user device.
                command += ["--no-sandbox", "--test-type", "--disable-dev-shm-usage"]
            process = subprocess.Popen([*command, action["url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import time
            time.sleep(0.5)
            if process.poll() not in (None, 0): raise RuntimeError("Browser failed to start")
        else: webbrowser.open(action["url"])
        return
    if system == "Linux":
        import pyautogui as gui
        gui.FAILSAFE = True
        if kind in {"click", "double_click", "right_click"}: gui.click(action["x"], action["y"], clicks=2 if kind == "double_click" else 1, button="right" if kind == "right_click" else "left")
        elif kind == "move": gui.moveTo(action["x"], action["y"])
        elif kind == "scroll":
            # Scroll the content under the given point, not wherever the pointer was left.
            if "x" in action and "y" in action: gui.moveTo(action["x"], action["y"])
            gui.scroll(int(action.get("amount", 1)))
        elif kind == "key": gui.hotkey(*KEYS[action["key"]].split("+"))
        elif kind == "hotkey":
            # Linux has no Command key: cmd means Ctrl, as in the single-key table.
            modifiers, name = chord(action["keys"])
            gui.hotkey(*["ctrl" if key == "cmd" else key for key in modifiers], KEYS.get(name, name.lower()))
        elif kind == "type":
            text = action.get("text", "")
            if all(32 <= ord(char) < 127 or char in "\n\t" for char in text):
                # Real key presses work in every app. Ctrl+V does not: a terminal reads it as "insert next key literally".
                gui.write(text, interval=0.005)
            else:
                # Unicode goes through the clipboard. Shift+Insert pastes in terminals, GTK apps and Chromium.
                # The private X display prevents task Bash from reading the clipboard.
                for selection in ("clipboard", "primary"):
                    subprocess.run(["xclip", "-selection", selection], input=text.encode(), check=True)
                gui.hotkey("shift", "insert")
        return
    if system == "Darwin":
        cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        class Point(ctypes.Structure): _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, Point, ctypes.c_uint32]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        def post(event):
            if not event: raise RuntimeError("Input permission unavailable")
            cg.CGEventPost(0, event); cf.CFRelease(event)
        if kind in {"click", "right_click", "double_click", "move"}:
            point = Point(action["x"], action["y"])
            button = 1 if kind == "right_click" else 0
            cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
            for click in range(2 if kind == "double_click" else 1):
                if kind == "move": post(cg.CGEventCreateMouseEvent(None, 5, point, 0))
                else:
                    for event_type in (3 if button else 1, 4 if button else 2):
                        event = cg.CGEventCreateMouseEvent(None, event_type, point, button)
                        cg.CGEventSetIntegerValueField(event, 1, click + 1)
                        post(event)
        elif kind == "type":
            raw = action.get("text", "").encode("utf-16-le")
            codes = (ctypes.c_uint16 * (len(raw)//2)).from_buffer_copy(raw)
            cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
            event = cg.CGEventCreateKeyboardEvent(None, 0, True)
            cg.CGEventKeyboardSetUnicodeString(event, len(codes), codes); post(event)
            post(cg.CGEventCreateKeyboardEvent(None, 0, False))
        elif kind == "key":
            code, flags = MAC_KEYS[action["key"]]
            cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            for down in (True,False):
                event = cg.CGEventCreateKeyboardEvent(None,code,down)
                cg.CGEventSetFlags(event,flags); post(event)
        elif kind == "hotkey":
            modifiers, name = chord(action["keys"])
            flags = sum(MAC_FLAGS[modifier] for modifier in set(modifiers))
            code = MAC_CHARS[name.lower()] if len(name) == 1 else MAC_KEYS[name][0]
            cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            for down in (True, False):
                event = cg.CGEventCreateKeyboardEvent(None, code, down)
                cg.CGEventSetFlags(event, flags); post(event)
        elif kind == "scroll":
            # Scroll the content under the given point, not wherever the pointer was left.
            if "x" in action and "y" in action:
                post(cg.CGEventCreateMouseEvent(None, 5, Point(action["x"], action["y"]), 0))
            # CGEventCreateScrollWheelEvent is variadic: on Apple silicon ctypes passes its wheel
            # value where the callee does not read it, and every scroll is a silent no-op. The
            # fixed-argument variant takes all three wheels as named parameters.
            cg.CGEventCreateScrollWheelEvent2.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                          ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
            cg.CGEventCreateScrollWheelEvent2.restype = ctypes.c_void_p
            post(cg.CGEventCreateScrollWheelEvent2(None, 1, 1, int(action.get("amount", 1)), 0, 0))
        else: raise RuntimeError("This Mac action is unavailable")
        return
    if system == "Windows":
        user = ctypes.windll.user32
        if kind in {"click", "double_click", "right_click", "move"}:
            user.SetCursorPos(action["x"], action["y"])
            if kind != "move":
                down, up = (8,16) if kind == "right_click" else (2,4)
                for _ in range(2 if kind == "double_click" else 1): user.mouse_event(down,0,0,0,0); user.mouse_event(up,0,0,0,0)
        elif kind == "scroll":
            if "x" in action and "y" in action: user.SetCursorPos(action["x"], action["y"])
            user.mouse_event(0x800,0,0,int(action.get("amount",1))*120,0)
        elif kind == "type":
            # SendInput Unicode avoids changing the user's clipboard.
            class Keyboard(ctypes.Structure): _fields_=[("vk",ctypes.c_ushort),("scan",ctypes.c_ushort),("flags",ctypes.c_ulong),("time",ctypes.c_ulong),("extra",ctypes.c_size_t)]
            class Union(ctypes.Union): _fields_=[("ki",Keyboard),("padding",ctypes.c_byte*32)]
            class Input(ctypes.Structure): _fields_=[("type",ctypes.c_ulong),("u",Union)]
            raw=action.get("text", "").encode("utf-16-le")
            for i in range(0,len(raw),2):
                code=int.from_bytes(raw[i:i+2],"little")
                for flag in (4,6):
                    item=Input(type=1,u=Union(ki=Keyboard(scan=code,flags=flag)))
                    if user.SendInput(1,ctypes.byref(item),ctypes.sizeof(item)) != 1: raise RuntimeError("Input permission unavailable")
        elif kind == "key":
            values=WINDOWS_KEYS[action["key"]]
            for key in values: user.keybd_event(key,0,0,0)
            for key in reversed(values): user.keybd_event(key,0,2,0)
        elif kind == "hotkey":
            # Windows has no Command key: cmd means Ctrl, as in the single-key table.
            modifiers, name = chord(action["keys"])
            values = [WINDOWS_MODIFIERS[key] for key in modifiers] + [ord(name.upper()) if len(name) == 1 else WINDOWS_KEYS[name][0]]
            for key in values: user.keybd_event(key,0,0,0)
            for key in reversed(values): user.keybd_event(key,0,2,0)
        return
    raise RuntimeError("Unsupported device platform")


APPLICATION_SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"


def mac_permissions(prompt=False):
    """Screen Recording and Accessibility state. ``prompt`` shows the system requests."""
    cg = ctypes.CDLL(APPLICATION_SERVICES)
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    cg.AXIsProcessTrusted.restype = ctypes.c_bool
    if prompt and not cg.CGPreflightScreenCaptureAccess():
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGRequestScreenCaptureAccess()
    if prompt and not cg.AXIsProcessTrusted():
        cf = ctypes.CDLL(CORE_FOUNDATION)
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        keys = (ctypes.c_void_p * 1)(ctypes.c_void_p.in_dll(cg, "kAXTrustedCheckOptionPrompt").value)
        values = (ctypes.c_void_p * 1)(ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value)
        options = cf.CFDictionaryCreate(None, keys, values, 1,
                                        ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
                                        ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
        cg.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        cg.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        cg.AXIsProcessTrustedWithOptions(options)
        cf.CFRelease(options)
    return {"screen": bool(cg.CGPreflightScreenCaptureAccess()), "input": bool(cg.AXIsProcessTrusted())}


def probe():
    """Whether this computer can be observed and controlled, and what the OS still has to allow."""
    system = platform.system()
    if system == "Darwin":
        return {"control": True, "permissions": mac_permissions()}
    if system == "Windows":
        return {"control": True, "permissions": None}
    if system == "Linux":
        from importlib.util import find_spec
        return {"control": bool(os.environ.get("DISPLAY")) and find_spec("pyautogui") is not None, "permissions": None}
    return {"control": False, "permissions": None}


def request_permissions():
    if platform.system() == "Darwin":
        mac_permissions(prompt=True)
    return probe()


def blocked(needs_input):
    """The waiting receipt while macOS has not granted what this request needs, else None."""
    if platform.system() != "Darwin": return None
    granted = mac_permissions()
    if not granted["screen"]: return {"waiting": True, "message": "Allow Screen Recording for this app in macOS System Settings."}
    if needs_input and not granted["input"]: return {"waiting": True, "message": "Allow Accessibility for this app in macOS System Settings."}
    return None


def execute(action):
    waiting = blocked(action["type"] != "observe")
    if waiting:
        return waiting
    before = observe()
    if action["type"] == "observe": return before
    if not same_screen(action, before):
        return {"status": "stale_observation", "message": "Screen changed; observe again"}
    for key, bound in (("x", before["width"]), ("y", before["height"])):
        if key in action and not 0 <= action[key] < bound: raise ValueError("Coordinate outside screen")
    # Coordinates are in the observation's pixels; a reduced observation maps back to the screen.
    scale = before.get("scale") or 1.0
    native_action({**action, **{key: int(action[key] * scale) for key in ("x", "y") if key in action}})
    return {**observe(settle()), "status": "input_executed"}


PLATFORMS = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}


def as_read(observation):
    """An observation in the ``read`` receipt shape (contract A.4, pixels mode)."""
    return {**observation, "mode": "pixels", "platform": PLATFORMS.get(platform.system(), "linux"),
            "skill": SKILLS["pixels"], "window": "screen", "app": None, "title": None, "windows": [],
            "snapshot_id": observation["observation_id"], "elements": None, "truncated": False,
            "invalidated_snapshot_ids": [], "degraded_reason": None, "media_type": "image/png"}


def read(command): return blocked(False) or as_read(observe())


def steps(command):
    """Pixel steps in order, stopping at the first failure, then the screen after. Only the first
    step is checked against the read the batch was planned on; later ones act on what came before."""
    batch, waiting = command.get("steps"), blocked(True)
    refusal = steps_refusal(batch, elements=False) or (waiting and {**waiting, "not_executed": True})
    if refusal:
        return refusal
    before = observe()
    planned = {"observation_id": command.get("snapshot_id"), "expected_signature": command.get("expected_signature")}
    if not same_screen(planned, before):
        report = {"index": 0, "type": batch[0]["type"], "ok": False, "effect": "refused", "message": "The screen changed since the read; read again"}
        return {"ok": False, "executed": 0, "failed_step": 0, "steps": [report], "after": as_read(before), "not_executed": True}
    scale = before.get("scale") or 1.0
    reports, failed = [], None
    for index, step in enumerate(batch):
        report = {"index": index, "type": step["type"]}
        if index: settle()  # The next step acts on what the previous one produced.
        try:
            if any(axis in step and not 0 <= step[axis] < before[size] for axis, size in (("x", "width"), ("y", "height"))):
                raise ValueError("Coordinate outside the screen")
            native_action(pixel_input(step, scale))
        except ValueError as exc:
            report.update(ok=False, effect="refused", message=str(exc))
        except Exception as exc:
            # The input may have been partly delivered: what it did is not known.
            report.update(ok=False, effect="unverifiable", message=str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        else: report.update(ok=True, effect="unverifiable")  # Synthetic input reports no result of its own.
        reports.append(report)
        if not report["ok"]:
            failed = index
            break
    return {"ok": failed is None, "executed": len(reports) if failed is None else failed, "failed_step": failed,
            "steps": reports, "after": as_read(observe(settle()))}


def pixel_input(step, scale):
    """One step as input: coordinates map back to the screen; a scroll direction sets the amount's sign."""
    action = {**step, **{axis: int(step[axis] * scale) for axis in ("x", "y") if axis in step}}
    if step["type"] == "scroll":
        if step.get("direction") in ("left", "right"): raise ValueError("Horizontal scrolling needs the elements mode")
        amount = step.get("amount", 5)
        action["amount"] = {"up": abs(amount), "down": -abs(amount)}.get(step.get("direction"), amount)
    if step["type"] == "hotkey": chord(step["keys"])  # Refused before any key is pressed.
    return action


LOCAL_REQUESTS = {"probe": probe, "request_permissions": request_permissions}
COMMANDS = {"read": read, "steps": steps}


def run(action):
    local = LOCAL_REQUESTS.get(action["type"])
    if local:
        return local()
    return COMMANDS.get(action["type"], execute)(action)

if __name__ == "__main__":
    try: print(json.dumps(run(json.loads(sys.argv[1]))))
    except Exception as exc:
        print(json.dumps({"status":"failed", "error":type(exc).__name__})); sys.exit(1)
