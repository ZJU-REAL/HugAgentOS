"""The macOS accessibility host (embedded Cua Driver), against a stand-in SDK.

What is checked: it only ever runs embedded, it reports why it cannot serve, a read and a
batch have the contract A.4 shape, every step becomes one background call of the tool
it names (and nothing else is reachable), and a batch stops at the first failure.
"""

import enum
import json
from types import SimpleNamespace

import pytest

from personal_agent.device_link import ui_host

# Every Cua tool a cloud command can reach through the host.
REACHABLE = {"list_windows", "get_window_state", "click", "double_click", "right_click", "move_cursor", "type_text",
             "set_value", "press_key", "hotkey", "scroll", "invoke_menu"}


class Mode(enum.Enum):
    EMBEDDED = 0
    DAEMON = 1


def result(text="ok", structured=None, error=False, code=None, effect=None, images=()):
    action = SimpleNamespace(effect=SimpleNamespace(name=effect.upper()), hint=None) if effect else None
    return SimpleNamespace(text=text, structured_json=json.dumps(structured) if structured is not None else None,
                           is_error=error, error_code=code, action=action, images=list(images))


WINDOWS = {"windows": [
    {"window_id": 9, "pid": 300, "app_name": "Finder", "title": "", "layer": 0, "z_index": 1, "is_on_screen": True},
    {"window_id": 7, "pid": 200, "app_name": "TextEdit", "title": "Untitled", "layer": 0, "z_index": 4, "is_on_screen": True},
    {"window_id": 5, "pid": 200, "app_name": "TextEdit", "title": "Old", "layer": 0, "z_index": None, "is_on_screen": False},
    {"window_id": 3, "pid": 100, "app_name": "Dock", "title": "", "layer": 20, "z_index": 9, "is_on_screen": True},
]}
STATE = {"snapshot_id": "s0000002a", "app_name": "TextEdit", "window_title": "Untitled", "truncated": False,
         "tree_markdown": "- [0] AXWindow \"Untitled\"\n  - [1] AXTextArea", "invalidated_snapshot_ids": ["s00000029"]}


class Driver:
    def __init__(self, mode=Mode.EMBEDDED):
        self.mode, self.calls, self.closed = mode, [], False
        self.answers = {}

    def execution_mode(self):
        return self.mode

    async def call_tool(self, name, arguments):
        self.calls.append((name, json.loads(arguments)))
        answer = self.answers.get(name)
        if callable(answer):
            return answer(json.loads(arguments))
        if answer is not None:
            return answer
        if name == "list_windows":
            return result(structured=WINDOWS)
        if name == "get_window_state":
            return result(structured=STATE)
        return result(structured={"effect": "confirmed"})

    async def shutdown(self):
        self.closed = True

    def tools(self):
        return [name for name, _ in self.calls]


def sdk(driver, accessibility=True, created=None):
    def create(options, embedded=False):
        assert options is None and embedded is True  # Never the daemon search.
        if created is not None:
            created.append(True)
        return driver

    return SimpleNamespace(
        CuaDriver=SimpleNamespace(create=create), DriverExecutionMode=Mode, __version__="test",
        current_mac_os_permission_status=lambda: SimpleNamespace(accessibility=accessibility, screen_recording=True))


def mac_host(driver=None, **options):
    driver = driver or Driver()
    return ui_host.UiHost(load=lambda: sdk(driver, **options), system=lambda: "Darwin"), driver


async def test_only_an_embedded_driver_on_a_mac_with_accessibility_serves(monkeypatch):
    monkeypatch.setenv("CUA_DRIVER_ALLOW_BRIEF_FOREGROUND", "1")
    host, _ = mac_host()
    assert await host.available() and host.reason is None
    assert ui_host.os.environ["CUA_DRIVER_EMBEDDED"] == "1"
    assert "CUA_DRIVER_ALLOW_BRIEF_FOREGROUND" not in ui_host.os.environ

    elsewhere = ui_host.UiHost(load=lambda: pytest.fail("must not load"), system=lambda: "Linux")
    assert not await elsewhere.available() and elsewhere.reason == "not_macos"

    def missing():
        raise ImportError("No module named cua_driver")

    absent = ui_host.UiHost(load=missing, system=lambda: "Darwin")
    assert not await absent.available() and absent.reason == "cua_driver_not_installed"

    daemon, driver = mac_host(Driver(mode=Mode.DAEMON))
    assert not await daemon.available() and daemon.reason == "cua_driver_not_embedded" and driver.closed


async def test_accessibility_granted_later_is_picked_up_without_a_restart():
    created, grant = [], {"accessibility": False}
    driver = Driver()
    host = ui_host.UiHost(load=lambda: SimpleNamespace(
        **{**vars(sdk(driver, created=created)),
           "current_mac_os_permission_status": lambda: SimpleNamespace(screen_recording=False, **grant)}),
        system=lambda: "Darwin")
    assert not await host.available() and host.reason == "accessibility_permission_missing"
    grant["accessibility"] = True
    assert await host.available()
    assert created == [True]  # One driver for the process.
    await host.close()
    assert driver.closed


async def test_a_read_without_a_window_reads_the_front_window():
    host, driver = mac_host()
    await host.available()
    read = await host.read({"type": "read", "window": None, "query": None, "since": None, "screenshot": None})
    assert read["mode"] == "elements" and read["platform"] == "macos" and read["skill"] == "pa-computer-macos"
    assert read["window"] == "200:7" and read["app"] == "TextEdit" and read["title"] == "Untitled"
    assert read["snapshot_id"] == "s0000002a" and read["elements"].startswith("- [0] AXWindow")
    assert read["invalidated_snapshot_ids"] == ["s00000029"] and read["truncated"] is False
    # On-screen windows, front to back; floating layers (the Dock) are not windows to act in, and
    # hidden windows are named only by their app.
    assert [item["window"] for item in read["windows"]] == ["200:7", "300:9"]
    assert read["background_apps"] == ["TextEdit"]
    assert "image_base64" not in read  # Elements reads carry no image unless asked.
    name, arguments = driver.calls[-1]
    assert name == "get_window_state" and arguments["pid"] == 200 and arguments["window_id"] == 7
    assert arguments["include_screenshot"] is False and arguments["max_elements"] == 250
    assert arguments["tree_format"] == "markdown" and arguments["screenshot_format"] == "jpeg"


async def test_a_read_with_query_since_and_screenshot():
    host, driver = mac_host()
    driver.answers["get_window_state"] = result(
        structured={**STATE, "tree_markdown": None, "tree_diff": "~ [1] AXTextArea value=\"hi\"",
                    "since_status": "diff", "screenshot_width": 1366, "screenshot_height": 854, "screenshot_scale": 2.0},
        images=[SimpleNamespace(data_base64="/9j/AAAA", mime_type="image/jpeg")])
    await host.available()
    read = await host.read({"window": "200:7", "query": "text", "since": "latest", "screenshot": True})
    assert read["elements"].startswith("~ [1]") and read["since_status"] == "diff"
    assert (read["image_base64"], read["media_type"], read["width"], read["height"], read["scale"]) == \
        ("/9j/AAAA", "image/jpeg", 1366, 854, 2.0)
    arguments = driver.calls[-1][1]
    assert arguments["query"] == "text" and arguments["since"] == "latest" and arguments["include_screenshot"] is True


async def test_a_read_says_why_a_screenshot_is_missing_and_reports_refusals():
    host, driver = mac_host()
    driver.answers["get_window_state"] = result(structured={**STATE, "screenshot_error": {"code": "direct_capture_consent_required"}})
    await host.available()
    read = await host.read({"window": "200:7", "screenshot": True})
    assert read["degraded_reason"] == "direct_capture_consent_required"

    driver.answers["get_window_state"] = result("window_id 8 not found", error=True, code="window_id_not_found")
    gone = await host.read({"window": "200:8"})
    assert gone["error"].startswith("window_id_not_found") and gone["windows"]
    bad = await host.read({"window": "TextEdit"})
    assert bad["error"].startswith("invalid_window") and bad["not_executed"]


@pytest.mark.parametrize("step, tool, expected", [
    ({"type": "click", "element": "s0000002a:3"}, "click", {"element_token": "s0000002a:3", "delivery_mode": "background"}),
    ({"type": "double_click", "x": 10, "y": 20}, "double_click", {"x": 10, "y": 20, "delivery_mode": "background"}),
    ({"type": "right_click", "element": "s0000002a:3"}, "right_click", {"delivery_mode": "background"}),
    ({"type": "type", "element": "s0000002a:1", "text": "你好"}, "type_text", {"text": "你好", "delivery_mode": "background"}),
    ({"type": "set_value", "element": "s0000002a:1", "text": "v"}, "set_value", {"value": "v", "element_token": "s0000002a:1"}),
    ({"type": "key", "key": "enter"}, "press_key", {"key": "return", "delivery_mode": "background"}),
    ({"type": "key", "key": "delete"}, "press_key", {"key": "forward_delete"}),
    ({"type": "key", "key": "ctrl+c"}, "press_key", {"key": "c", "modifiers": ["cmd"]}),
    ({"type": "key", "key": "alt+left"}, "press_key", {"key": "left", "modifiers": ["option"]}),
    ({"type": "hotkey", "keys": ["cmd", "shift", "T"]}, "hotkey", {"keys": ["cmd", "shift", "t"], "delivery_mode": "background"}),
    ({"type": "hotkey", "keys": ["escape"]}, "press_key", {"key": "escape"}),
    ({"type": "scroll", "amount": -7}, "scroll", {"direction": "down", "amount": 7, "delivery_mode": "background"}),
    ({"type": "scroll", "amount": 100, "direction": "left"}, "scroll", {"direction": "left", "amount": 50}),
    ({"type": "move", "x": 5, "y": 6}, "move_cursor", {"x": 5, "y": 6}),
    ({"type": "menu", "path": ["Format", "Make Plain Text"]}, "invoke_menu", {"path": ["Format", "Make Plain Text"]}),
])
async def test_each_step_is_one_background_call_of_its_tool(step, tool, expected):
    host, driver = mac_host()
    await host.available()
    batch = await host.steps({"type": "steps", "window": "200:7", "snapshot_id": "s0000002a", "steps": [step]})
    assert batch["ok"] and batch["executed"] == 1
    name, arguments = driver.calls[0]
    assert name == tool
    assert {key: arguments[key] for key in expected} == expected
    if tool not in ("move_cursor",):
        assert (arguments["pid"], arguments["window_id"]) == (200, 7)
    if tool in ("set_value", "invoke_menu", "move_cursor"):
        assert "delivery_mode" not in arguments  # These tools take none; extra arguments are refused.
    else:
        assert arguments["delivery_mode"] == "background"


async def test_a_batch_stops_at_the_first_failure_and_reads_what_changed():
    host, driver = mac_host()
    driver.answers["click"] = result("clicked", structured={}, effect="confirmed")
    driver.answers["type_text"] = result("Refused: no focused text field", structured={
        "hint": "Click the field first", "next_call": {"tool": "click"}}, error=True, code="no_focus", effect="refused")
    await host.available()
    batch = await host.steps({"type": "steps", "window": "200:7", "snapshot_id": "s0000002a", "steps": [
        {"type": "click", "element": "s0000002a:1"}, {"type": "type", "text": "x"}, {"type": "key", "key": "enter"}]})
    assert not batch["ok"] and batch["executed"] == 1 and batch["failed_step"] == 1
    assert [(step["index"], step["ok"], step["effect"]) for step in batch["steps"]] == [(0, True, "confirmed"), (1, False, "refused")]
    assert batch["steps"][1]["hint"] == "Click the field first" and batch["steps"][1]["next_call"] == {"tool": "click"}
    assert batch["steps"][1]["code"] == "no_focus"
    assert "press_key" not in driver.tools()  # Nothing after the failure ran.
    after = batch["after"]
    assert after["mode"] == "elements" and after["window"] == "200:7"
    assert driver.calls[-1][0] == "get_window_state" and driver.calls[-1][1]["since"] == "latest"


async def test_a_driver_error_mid_batch_is_an_unknown_effect_and_stops():
    host, driver = mac_host()

    def crash(arguments):
        raise RuntimeError("tokio worker gone")

    driver.answers["type_text"] = crash
    await host.available()
    batch = await host.steps({"window": "200:7", "steps": [{"type": "type", "text": "x"}, {"type": "key", "key": "enter"}]})
    assert batch["failed_step"] == 0 and batch["steps"][0]["effect"] == "unverifiable"
    assert "press_key" not in driver.tools()


async def test_an_invalid_batch_runs_nothing():
    host, driver = mac_host()
    await host.available()
    for command in (
        {"window": "200:7", "steps": [{"type": "run_script", "text": "do shell script"}]},
        {"window": "200:7", "steps": [{"type": "click", "element": "not-a-token"}]},
        {"window": "200:7", "steps": [{"type": "click"}]},
        {"window": None, "steps": [{"type": "click", "element": "s0000002a:3"}]},
        {"window": "200:7", "steps": [{"type": "key", "key": "enter"}] * 17},
    ):
        refused = await host.steps(command)
        assert refused["not_executed"] and "error" in refused
    assert driver.calls == []


async def test_open_browser_hands_the_address_over_in_the_background(monkeypatch):
    opened = []

    class Process:
        async def wait(self):
            return 0

    async def spawn(*argv, **kwargs):
        opened.append(argv)
        return Process()

    monkeypatch.setattr(ui_host.asyncio, "create_subprocess_exec", spawn)
    host, driver = mac_host()
    await host.available()
    batch = await host.steps({"window": None, "steps": [{"type": "open_browser", "url": "https://example.com"}]})
    assert batch["ok"] and opened == [("/usr/bin/open", "-g", "https://example.com")]
    assert batch["after"]["window"] == "200:7"  # No window named: the front one after.


async def test_only_contract_steps_reach_the_driver():
    """Whatever a batch holds, the host calls only the tools its step mapping names."""
    host, driver = mac_host()
    await host.available()
    steps = [{"type": kind, "element": "s0000002a:1", "text": "t"} for kind in ("click", "type", "set_value")]
    steps += [{"type": "key", "key": "tab"}, {"type": "hotkey", "keys": ["cmd", "a"]}, {"type": "scroll", "amount": 1},
              {"type": "menu", "path": ["Edit"]}, {"type": "move", "x": 1, "y": 1}]
    await host.steps({"window": "200:7", "steps": [{key: value for key, value in step.items()
                                                    if key != "text" or step["type"] in ("type", "set_value")}
                                                   for step in steps]})
    assert set(driver.tools()) <= REACHABLE
    assert "run_steps" not in driver.tools() and "run_script" not in driver.tools()


async def test_only_windows_on_screen_are_listed_with_titles():
    host, _ = mac_host()
    await host.available()
    out = await host.read({"type": "read"})
    assert [w["title"] for w in out["windows"]] == ["Untitled", ""]
    assert all(w["on_screen"] for w in out["windows"])
    # The hidden TextEdit window's title stays on this computer; only the app is named.
    assert out["background_apps"] == ["TextEdit"] and "Old" not in json.dumps(out)


def test_element_rows_identify_by_role_title_and_id_not_value():
    rows = ui_host.element_rows('- [0] AXWindow "e2e" [id=_NS:34 actions=[raise]]\n'
                                '  - [1] AXTextArea = "typed text" [id=First Text View]\n'
                                '  - [2] AXButton [actions=[press]]\n'
                                '  - [5] AXMenuButton "Edited" (document actions)')
    assert rows == {"0": ("AXWindow", "e2e", "_NS:34"), "1": ("AXTextArea", "", "First Text View"),
                    "2": ("AXButton", "", ""), "5": ("AXMenuButton", "Edited", "")}


def _tree(snapshot, markdown):
    return result(structured={**STATE, "snapshot_id": snapshot, "tree_markdown": markdown})


async def test_a_batch_planned_on_an_older_read_is_pointed_at_the_same_element():
    host, driver = mac_host()
    await host.available()
    driver.answers["get_window_state"] = _tree("s00000003", '- [0] AXWindow "Note"\n  - [1] AXTextArea [id=Body]')
    await host.read({"type": "read", "window": "200:7"})
    # A later read (the agent's own, or an approval that came late) renumbers the window.
    driver.answers["get_window_state"] = _tree("s00000004", '- [0] AXWindow "Note"\n  - [4] AXButton "OK"\n  - [6] AXTextArea [id=Body]')
    await host.read({"type": "read", "window": "200:7"})
    out = await host.steps({"type": "steps", "window": "200:7", "snapshot_id": "s00000003",
                            "steps": [{"type": "type", "element": "s00000003:1", "text": "hi"}]})
    assert out["ok"]
    typed = [args for name, args in driver.calls if name == "type_text"]
    assert typed[0]["element_token"].endswith(":6")


async def test_an_element_that_cannot_be_matched_once_refuses_the_batch():
    host, driver = mac_host()
    await host.available()
    driver.answers["get_window_state"] = _tree("s00000003", '- [0] AXWindow "Note"\n  - [1] AXButton "OK"')
    await host.read({"type": "read", "window": "200:7"})
    driver.answers["get_window_state"] = _tree("s00000004", '- [0] AXWindow "Note"\n  - [2] AXButton "OK"\n  - [3] AXButton "OK"')
    await host.read({"type": "read", "window": "200:7"})
    out = await host.steps({"type": "steps", "window": "200:7", "snapshot_id": "s00000003",
                            "steps": [{"type": "click", "element": "s00000003:1"}]})
    assert out["not_executed"] and out["error"].startswith("stale_elements")
    assert "click" not in driver.tools()


async def test_a_current_token_is_not_re_read():
    host, driver = mac_host()
    await host.available()
    await host.read({"type": "read", "window": "200:7"})
    reads = driver.tools().count("get_window_state")
    await host.steps({"type": "steps", "window": "200:7", "snapshot_id": "s0000002a",
                      "steps": [{"type": "type", "element": "s0000002a:1", "text": "hi"}]})
    # Only the read that becomes ``after``.
    assert driver.tools().count("get_window_state") == reads + 1


async def test_a_read_without_a_window_skips_a_front_overlay_it_cannot_read():
    host, driver = mac_host()
    await host.available()

    def state(arguments):
        if arguments["pid"] == 200:
            return result(text="window has no tree", error=True, code="ax_unavailable")
        return result(structured={**STATE, "app_name": "Finder"})
    driver.answers["get_window_state"] = state
    out = await host.read({"type": "read"})
    assert "error" not in out and out["window"] == "300:9" and out["app"] == "Finder"
