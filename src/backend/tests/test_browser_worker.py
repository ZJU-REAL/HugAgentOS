"""Real Chromium tests of the public worker command/stream boundary."""
import asyncio
import json
import sys
import uuid
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1] / "plugin_bundles/marketplace/browser-automation/runtime"))
from browser_worker.protocol import Command
from browser_worker.commands import execute
from browser_worker.session import BrowserSession

async def command(session, action, params=None, actor="agent", connection_id="", command_id=None):
    return await execute(session, Command(id=command_id or uuid.uuid4().hex, actor=actor, connection_id=connection_id, action=action, params=params or {}))

@pytest.fixture
async def browser():
    session = BrowserSession({"chromium_sandbox": False, "allowed_hosts": ["127.0.0.1"]})
    await session.start()
    yield session
    if not session.closed:
        await session.close()

async def test_actual_browser_control_input_idempotency_and_private_observation(browser):
    page = browser.pages[browser.active_tab]
    await page.set_content('<input aria-label="姓名"><button onclick="this.dataset.count=Number(this.dataset.count||0)+1">提交</button>')
    await command(browser, "fill", {"role": "textbox", "name": "姓名", "text": "浏览器验证"})
    assert await page.locator("input").input_value() == "浏览器验证"
    identifier = uuid.uuid4().hex
    await command(browser, "click", {"role": "button", "name": "提交"}, command_id=identifier)
    await command(browser, "click", {"role": "button", "name": "提交"}, command_id=identifier)
    assert await page.locator("button").get_attribute("data-count") == "1"
    await command(browser, "take_control", {"private": True}, "user", "viewer")
    with pytest.raises(ValueError, match="control_not_owned"):
        await command(browser, "snapshot")
    await page.locator("input").focus()
    await command(browser, "input", {"kind": "text", "text": "中文", "viewport_revision": browser.viewport_revision}, "user", "viewer")
    assert "中文" in await page.locator("input").input_value()
    with pytest.raises(ValueError, match="stale_viewport"):
        await command(browser, "input", {"kind": "down", "x": 5, "y": 5, "viewport_revision": -1}, "user", "viewer")
    await command(browser, "release_control", actor="user", connection_id="viewer")
    assert "姓名" in (await command(browser, "snapshot"))["snapshot"]

async def test_actual_browser_stream_tabs_and_network_policy(browser):
    events = browser.subscribe()
    first = await anext(events)
    assert isinstance(first, bytes)
    await events.aclose()
    await command(browser, "new_tab")
    assert len((await command(browser, "state"))["tabs"]) == 2
    with pytest.raises(ValueError, match="unsupported_url"):
        await command(browser, "navigate", {"url": "file:///etc/passwd"})
    with pytest.raises(ValueError, match="blocked_destination"):
        await browser.policy.check("http://169.254.169.254/")

async def test_resize_keeps_live_frames_and_dialog_can_interrupt_pending_click(browser):
    page = browser.pages[browser.active_tab]
    await page.set_content('<button onclick="alert(\'verified\')">Dialog</button>')
    events = browser.subscribe()
    await anext(events)
    await command(browser, "resize", {"width": 900, "height": 600})
    revision = browser.viewport_revision
    async def next_frame():
        while True:
            data = await anext(events)
            length = int.from_bytes(data[:4], "big")
            header = json.loads(data[4:4+length])
            if header["type"] == "frame" and header["viewport_revision"] == revision:
                return header
    assert (await asyncio.wait_for(next_frame(), 5))["viewport"] == {"width": 900, "height": 600}
    pending = asyncio.create_task(command(browser, "click", {"role": "button", "name": "Dialog"}))
    for _ in range(100):
        if browser.dialogs:
            break
        await asyncio.sleep(.02)
    assert browser.dialogs
    await command(browser, "dialog", {"accept": True})
    await asyncio.wait_for(pending, 5)
    await events.aclose()

async def test_slow_viewer_keeps_latest_frame_and_never_silently_drops_control():
    from browser_worker.viewer import Viewer
    viewer = Viewer()
    viewer.put(b"old", frame=True)
    viewer.put(b"new", frame=True)
    viewer.put(b"control")
    assert await viewer.get() == b"control"
    assert await viewer.get() == b"new"
    for _ in range(33):
        viewer.put(b"control")
    with pytest.raises(ValueError, match="slow_consumer"):
        await viewer.get()

async def test_private_allowlist_never_allows_metadata_or_mapped_link_local(browser):
    from browser_worker.network import NetworkPolicy
    policy = NetworkPolicy(["169.254.169.254", "::ffff:169.254.169.254"], True)
    for address in ["169.254.169.254", "::ffff:169.254.169.254"]:
        with pytest.raises(ValueError, match="blocked_destination"):
            await policy.addresses(address, 80)

async def test_failed_stream_initialization_releases_viewer(browser, monkeypatch):
    async def failure():
        raise ValueError("fixture_stream_failure")
    monkeypatch.setattr(browser, "start_stream", failure)
    events = browser.subscribe()
    with pytest.raises(ValueError, match="fixture_stream_failure"):
        await anext(events)
    assert not browser.viewers


async def test_configured_dns_checks_real_addresses_and_caches_bounded_answers(monkeypatch):
    import httpx
    from browser_worker.network import NetworkPolicy
    original = httpx.AsyncClient
    requests = []
    def answer(request):
        requests.append(request)
        kind = int(request.url.params["type"])
        data = [{"type": 1, "data": "93.184.215.14", "TTL": 120}] if kind == 1 else []
        return httpx.Response(200, json={"Status": 0, "Answer": data})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(answer), **kwargs))
    policy = NetworkPolicy(dns_resolver_url="https://resolver.invalid/resolve")
    addresses = await policy.addresses("public.invalid", 443)
    assert addresses[0][4] == ("93.184.215.14", 443)
    assert len(requests) == 2
    assert all(r.url.params["edns_client_subnet"] == "0.0.0.0/0" for r in requests)
    assert (await policy.addresses("public.invalid", 80))[0][4][1] == 80
    assert len(requests) == 2


async def test_configured_dns_never_bypasses_private_or_metadata_protection(monkeypatch):
    import httpx
    from browser_worker.network import NetworkPolicy
    original = httpx.AsyncClient
    address = "10.0.0.1"
    def answer(request):
        data = [{"type": 1, "data": address, "TTL": 0}] if request.url.params["type"] == "1" else []
        return httpx.Response(200, json={"Status": 0, "Answer": data})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(answer), **kwargs))
    policy = NetworkPolicy(dns_resolver_url="https://resolver.invalid/resolve")
    with pytest.raises(ValueError, match="private_destination_not_allowed"):
        await policy.addresses("private.invalid", 443)
    address = "169.254.169.254"
    with pytest.raises(ValueError, match="blocked_destination"):
        await policy.addresses("metadata.invalid", 443)


async def test_configured_dns_errors_do_not_fall_back_to_fake_ip(monkeypatch):
    import httpx
    from browser_worker.network import NetworkPolicy
    original = httpx.AsyncClient
    def failure(request):
        return httpx.Response(503)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(failure), **kwargs))
    with pytest.raises(OSError, match="dns_resolution_failed"):
        await NetworkPolicy(dns_resolver_url="https://resolver.invalid/resolve").addresses("test.invalid", 443)
    with pytest.raises(ValueError, match="invalid_dns_resolver"):
        NetworkPolicy(dns_resolver_url="http://resolver.invalid/resolve")


async def test_viewer_resize_without_control_preserves_agent_and_rejects_other_viewer(browser):
    result = await command(browser, "resize", {"width": 420, "height": 920}, "user", "viewer")
    assert result["ok"]
    assert browser.pages[browser.active_tab].viewport_size == {"width": 420, "height": 920}
    assert browser.controller == "agent"
    assert (await command(browser, "state"))["viewport_limits"]["min_width"] == 160
    await command(browser, "take_control", {"private": True}, "user", "viewer")
    with pytest.raises(ValueError, match="control_not_owned"):
        await command(browser, "resize", {"width": 500, "height": 600}, "user", "other")
    with pytest.raises(ValueError, match="control_not_owned"):
        await command(browser, "resize", {"width": 500, "height": 600}, "user")
    with pytest.raises(ValueError, match="invalid_viewport"):
        await command(browser, "resize", {"width": 1, "height": 600}, "user", "viewer")


async def test_automatic_user_control_can_answer_dialog_while_agent_click_pending(browser):
    page = browser.pages[browser.active_tab]
    await page.set_content('<title>Dialog test</title><button onclick="alert(&quot;Answer me&quot;)">Open</button>')
    for _ in range(100):
        if browser.titles.get(browser.active_tab) == "Dialog test":
            break
        await asyncio.sleep(.01)
    assert browser.titles.get(browser.active_tab) == "Dialog test"
    pending = asyncio.create_task(command(browser, "click", {"role": "button", "name": "Open"}))
    for _ in range(100):
        if browser.dialogs:
            break
        await asyncio.sleep(.02)
    assert browser.dialogs
    state = await asyncio.wait_for(command(browser, "take_control", {"private": True}, "user", "viewer"), 2)
    assert state["tabs"][0]["title"] == "Dialog test"
    await command(browser, "dialog", {"accept": True}, "user", "viewer")
    await asyncio.wait_for(pending, 5)


async def test_first_wheel_targets_hovered_scroll_container_without_prior_click(browser):
    page = browser.pages[browser.active_tab]
    await page.set_content('<div id="panel" style="margin:100px;width:400px;height:250px;overflow:auto"><div style="height:2000px">Scroll me</div></div>')
    await page.mouse.move(0, 0)
    await command(browser, "take_control", {"private": True}, "user", "viewer")
    await command(browser, "input", {"kind": "wheel", "x": 200, "y": 200, "dx": 0, "dy": 300, "viewport_revision": browser.viewport_revision}, "user", "viewer")
    await page.wait_for_timeout(150)
    assert await page.locator("#panel").evaluate("node => node.scrollTop") > 0

async def test_visible_native_scrollbar_and_same_size_resize_keeps_input_revision(browser):
    page = browser.pages[browser.active_tab]
    await page.set_content('<style>body{height:3000px}</style>Scroll')
    gutter = await page.evaluate("innerWidth - document.documentElement.clientWidth")
    if gutter > 0:
        await page.mouse.move(page.viewport_size["width"] - 5, 30)
        await page.mouse.down()
        await page.mouse.move(page.viewport_size["width"] - 5, 400, steps=10)
        await page.mouse.up()
    else:  # macOS can use overlay scrollbars with no reserved layout width.
        await page.mouse.move(100, 100)
        await page.mouse.wheel(0, 400)
        await page.wait_for_function("scrollY > 0")
    assert await page.evaluate("scrollY") > 0
    revision = browser.viewport_revision
    await command(browser, "resize", page.viewport_size, "user", "viewer")
    assert browser.viewport_revision == revision

async def test_manual_navigation_does_not_wait_for_slow_page_scripts(browser):
    release = asyncio.Event()
    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            if b"GET /slow.js " in request:
                await release.wait()
                body = b"window.loaded = true;"
                mime = b"text/javascript"
            else:
                body = b'<title>Slow page</title><script src="/slow.js"></script><body style="height:3000px">Ready</body>'
                mime = b"text/html"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + mime + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await command(browser, "take_control", {"private": True}, "user", "viewer")
        result = await asyncio.wait_for(command(browser, "navigate",
            {"url": f"http://127.0.0.1:{port}/"}, "user", "viewer"), 2)
        assert result["ok"]
    finally:
        release.set()
        server.close()
        await server.wait_closed()


async def test_busy_background_titles_do_not_delay_actions_or_state(browser):
    import time
    for _ in range(2):
        await command(browser, "new_tab")
    active = browser.pages[browser.active_tab]
    background = [page for page in browser.pages.values() if page != active]
    for page in background:
        await page.evaluate("setTimeout(() => { const end = performance.now() + 2500; while (performance.now() < end) {} }, 100)")
    await asyncio.sleep(.2)
    start = time.perf_counter()
    await command(browser, "key", {"key": "Shift"})
    await command(browser, "state")
    assert time.perf_counter() - start < .5


async def test_cached_titles_follow_script_changes_and_page_close(browser):
    page = browser.pages[browser.active_tab]
    tab = browser.active_tab
    await page.set_content("<title>Initial title</title>")
    async def wait_title(expected):
        for _ in range(100):
            state = await command(browser, "state")
            if next(row for row in state["tabs"] if row["id"] == tab)["title"] == expected:
                return
            await asyncio.sleep(.03)
        pytest.fail("Chromium title metadata did not update")
    await wait_title("Initial title")
    await page.evaluate("document.title = 'Changed title'")
    await wait_title("Changed title")
    await command(browser, "new_tab")
    await page.close()
    for _ in range(100):
        if tab not in browser.pages:
            break
        await asyncio.sleep(.01)
    assert tab not in browser.titles
    assert all(row["id"] != tab for row in (await command(browser, "state"))["tabs"])


async def test_title_cache_navigation_duplicate_urls_and_pending_close(browser):
    url = "http://127.0.0.1:8765/title-test"
    await browser.context.route(url, lambda route: route.fulfill(body="<title>Navigation title</title>", content_type="text/html"))
    await command(browser, "navigate", {"url": url})
    first = browser.active_tab
    await command(browser, "new_tab", {"url": url})
    second = browser.active_tab
    await browser.pages[second].evaluate("document.title = 'Second page title'")
    for _ in range(150):
        titles = {row["id"]: row["title"] for row in (await command(browser, "state"))["tabs"]}
        if titles.get(first) == "Navigation title" and titles.get(second) == "Second page title":
            break
        await asyncio.sleep(.02)
    assert titles[first] == "Navigation title"
    assert titles[second] == "Second page title"
    await browser.pages[first].evaluate("setTimeout(() => { const end = performance.now() + 3000; while (performance.now() < end) {} }, 100)")
    await asyncio.sleep(.2)
    browser.schedule_title(first, browser.pages[first])
    await asyncio.sleep(.05)
    assert first in browser.title_tasks
    tasks = tuple(browser.tasks)
    await asyncio.wait_for(browser.close(), 2)
    assert not browser.title_tasks
    assert all(task.done() for task in tasks)


async def test_large_viewport_stream_keeps_bottom_right_corner(browser):
    import io
    from PIL import Image
    page = browser.pages[browser.active_tab]
    await command(browser, "resize", {"width": 1920, "height": 1440})
    await page.set_content('<style>body{margin:0}i{position:fixed;width:80px;height:80px;background:red;right:0;bottom:0}</style><i></i>')
    events = browser.subscribe()
    try:
        while True:
            data = await asyncio.wait_for(anext(events), 5)
            size = int.from_bytes(data[:4], "big")
            header = json.loads(data[4:4+size])
            if header["type"] != "frame":
                continue
            image = Image.open(io.BytesIO(data[4+size:])).convert("RGB")
            red, green, blue = image.getpixel((image.width-20, image.height-20))
            assert red > 200 and green < 50 and blue < 50
            if image.width < 1920:  # Check a capped live frame, not only the initial screenshot.
                break
    finally:
        await events.aclose()


async def test_page_created_popup_becomes_active_and_streams_its_frame(browser):
    opener = browser.pages[browser.active_tab]
    await opener.set_content('<button onclick="window.open(\'about:blank\')">Open tab</button>')
    events = browser.subscribe()
    await anext(events)
    try:
        await command(browser, "click", {"role": "button", "name": "Open tab"})
        async def popup_frame():
            while True:
                data = await anext(events)
                size = int.from_bytes(data[:4], "big")
                header = json.loads(data[4:4+size])
                if header["type"] == "frame" and header["tab_id"] != "1":
                    return header
        frame = await asyncio.wait_for(popup_frame(), 3)
        state = await command(browser, "state")
        assert len(state["tabs"]) == 2
        assert state["active_tab"] == "2"
        assert frame["tab_id"] == state["active_tab"]
        assert frame["viewport_revision"] == state["viewport_revision"]
    finally:
        await events.aclose()


async def test_closing_active_tab_selects_right_then_left_and_preserves_background(browser):
    await command(browser, "new_tab")
    await command(browser, "new_tab")
    await command(browser, "select_tab", {"tab_id": "2"})
    await command(browser, "close_tab", {"tab_id": "2"})
    assert (await command(browser, "state"))["active_tab"] == "3"
    await command(browser, "close_tab", {"tab_id": "1"})
    state = await command(browser, "state")
    assert state["active_tab"] == "3"
    assert [row["id"] for row in state["tabs"]] == ["3"]
    await command(browser, "new_tab")
    await command(browser, "close_tab", {"tab_id": "4"})
    assert (await command(browser, "state"))["active_tab"] == "3"
    await command(browser, "close_tab", {"tab_id": "3"})
    state = await command(browser, "state")
    assert state["tabs"] == []
    assert state["active_tab"] == ""
    assert (await command(browser, "new_tab"))["tab_id"] == "5"
    assert (await command(browser, "state"))["active_tab"] == "5"


async def test_subscription_screenshot_cannot_be_relabelled_after_tab_switch(browser, monkeypatch):
    import io
    from PIL import Image
    page = browser.pages[browser.active_tab]
    await page.set_content('<style>body{margin:0;background:red}</style>')
    captured, release = asyncio.Event(), asyncio.Event()
    screenshot = page.screenshot
    async def delayed_screenshot(**kwargs):
        data = await screenshot(**kwargs)
        captured.set()
        await release.wait()
        return data
    monkeypatch.setattr(page, "screenshot", delayed_screenshot)
    events = browser.subscribe()
    pending = asyncio.create_task(anext(events))
    try:
        await asyncio.wait_for(captured.wait(), 3)
        await command(browser, "new_tab")
        release.set()
        data = await asyncio.wait_for(pending, 3)
        while True:
            size = int.from_bytes(data[:4], "big")
            header = json.loads(data[4:4+size])
            if header["type"] == "frame" and header["tab_id"] == "2":
                image = Image.open(io.BytesIO(data[4+size:])).convert("RGB")
                red, green, blue = image.getpixel((20, 20))
                assert green > 200 and blue > 200, "New blank tab must not display the old red page"
                break
            data = await asyncio.wait_for(anext(events), 3)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await events.aclose()
