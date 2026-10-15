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
import subprocess
import sys
import tempfile
from pathlib import Path

# The complete action vocabulary. The cloud validates against it and pairs devices with it.
ACTIONS = frozenset({"observe", "click", "double_click", "right_click", "move", "type", "key", "scroll", "open_browser"})
KEYS = {"enter": "enter", "tab": "tab", "escape": "esc", "backspace": "backspace", "ctrl+a": "ctrl+a", "ctrl+c": "ctrl+c", "ctrl+v": "ctrl+v", "alt+left": "alt+left",
        "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end", "delete": "delete", "pageup": "pageup", "pagedown": "pagedown",
        "ctrl+l": "ctrl+l", "ctrl+t": "ctrl+t", "ctrl+w": "ctrl+w", "ctrl+z": "ctrl+z", "ctrl+x": "ctrl+x", "ctrl+r": "ctrl+r", "ctrl+f": "ctrl+f"}


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


def same_screen(action, before):
    """True when the screen still matches the observation that the action refers to.
    The backend adds expected_signature. Without it, only an exact match is accepted."""
    if action.get("observation_id") == before["observation_id"]: return True
    old, new = str(action.get("expected_signature") or ""), before["signature"]
    if len(old) != len(new) or not old: return False
    changed = sum(abs(int(a, 16) - int(b, 16)) > 1 for a, b in zip(old, new))
    return changed <= len(old) * 0.02


def observe():
    image = screenshot()
    data = io.BytesIO()
    image.save(data, format="PNG")
    pixels = data.getvalue()
    return {"image_base64": base64.b64encode(pixels).decode(), "width": image.width, "height": image.height,
            "observation_id": hashlib.sha256(pixels).hexdigest()[:32], "signature": signature(image), "platform": platform.system()}


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
            keys = {"enter":(36,0),"tab":(48,0),"escape":(53,0),"backspace":(51,0),
                "ctrl+a":(0,1<<20),"ctrl+c":(8,1<<20),"ctrl+v":(9,1<<20),"alt+left":(123,1<<19),
                "up":(126,0),"down":(125,0),"left":(123,0),"right":(124,0),"home":(115,0),"end":(119,0),"delete":(117,0),"pageup":(116,0),"pagedown":(121,0),
                "ctrl+l":(37,1<<20),"ctrl+t":(17,1<<20),"ctrl+w":(13,1<<20),"ctrl+z":(6,1<<20),"ctrl+x":(7,1<<20),"ctrl+r":(15,1<<20),"ctrl+f":(3,1<<20)}
            code, flags = keys[action["key"]]
            cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            for down in (True,False):
                event = cg.CGEventCreateKeyboardEvent(None,code,down)
                cg.CGEventSetFlags(event,flags); post(event)
        elif kind == "scroll":
            cg.CGEventCreateScrollWheelEvent.argtypes = [ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32]
            cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
            post(cg.CGEventCreateScrollWheelEvent(None,1,1,ctypes.c_int(int(action.get("amount",1)))))
        else: raise RuntimeError("This Mac action is unavailable")
        return
    if system == "Windows":
        user = ctypes.windll.user32
        if kind in {"click", "double_click", "right_click", "move"}:
            user.SetCursorPos(action["x"], action["y"])
            if kind != "move":
                down, up = (8,16) if kind == "right_click" else (2,4)
                for _ in range(2 if kind == "double_click" else 1): user.mouse_event(down,0,0,0,0); user.mouse_event(up,0,0,0,0)
        elif kind == "scroll": user.mouse_event(0x800,0,0,int(action.get("amount",1))*120,0)
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
            mapping={"enter":[13],"tab":[9],"escape":[27],"backspace":[8],"ctrl+a":[17,65],"ctrl+c":[17,67],"ctrl+v":[17,86],"alt+left":[18,37],
                "up":[38],"down":[40],"left":[37],"right":[39],"home":[36],"end":[35],"delete":[46],"pageup":[33],"pagedown":[34],
                "ctrl+l":[17,76],"ctrl+t":[17,84],"ctrl+w":[17,87],"ctrl+z":[17,90],"ctrl+x":[17,88],"ctrl+r":[17,82],"ctrl+f":[17,70]}
            values=mapping[action["key"]]
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


def execute(action):
    if platform.system() == "Darwin":
        granted = mac_permissions()
        if not granted["screen"]:
            return {"waiting": True, "message": "Allow Screen Recording for this app in macOS System Settings."}
        if action["type"] != "observe" and not granted["input"]:
            return {"waiting": True, "message": "Allow Accessibility for this app in macOS System Settings."}
    before = observe()
    if action["type"] == "observe": return before
    if not same_screen(action, before):
        return {"status": "stale_observation", "message": "Screen changed; observe again"}
    for key, bound in (("x", before["width"]), ("y", before["height"])):
        if key in action and not 0 <= action[key] < bound: raise ValueError("Coordinate outside screen")
    native_action(action)
    import time
    time.sleep(0.25)
    return {**observe(), "status": "input_executed"}


LOCAL_REQUESTS = {"probe": probe, "request_permissions": request_permissions}


def run(action):
    local = LOCAL_REQUESTS.get(action["type"])
    return local() if local else execute(action)

if __name__ == "__main__":
    try: print(json.dumps(run(json.loads(sys.argv[1]))))
    except Exception as exc:
        print(json.dumps({"status":"failed", "error":type(exc).__name__})); sys.exit(1)
