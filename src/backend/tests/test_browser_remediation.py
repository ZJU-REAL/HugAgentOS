"""Browser failures preserve definite outcomes and structured transport errors."""
import asyncio
import sys
from pathlib import Path
import httpx
import pytest
from fastapi import HTTPException
sys.path.insert(0, str(Path(__file__).parents[1] / "plugin_bundles/marketplace/browser-automation/runtime"))
from tests.test_browser_worker import browser

@pytest.mark.asyncio
async def test_locator_and_dns_errors_are_specific(browser, monkeypatch):
    from browser_worker import server
    monkeypatch.setattr(server, "BrowserSession", lambda _: browser)
    await browser.pages[browser.active_tab].set_content("<button>A</button><button>B</button>")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app({"token": "probe"}), raise_app_exceptions=False), base_url="http://probe", headers={"x-hugagent-resource-token": "probe"}) as client:
        for selector, code in (("#missing", "element_not_found"), ("button", "ambiguous_locator")):
            response = await asyncio.wait_for(client.post("/command", json={"id": code, "actor": "agent", "action": "click", "params": {"selector": selector}}), 2)
            assert response.json()["detail"] == code
        async def dns_failure(url):
            raise OSError("dns_name_unavailable")
        monkeypatch.setattr(browser.policy, "check", dns_failure)
        response = await client.post("/command", json={"id": "dns", "actor": "agent", "action": "navigate", "params": {"url": "https://internal.invalid"}})
        assert response.status_code == 502
        assert response.json()["detail"] == "dns_failed"

@pytest.mark.asyncio
async def test_non_json_runtime_error_is_safe():
    from core.plugins.resources.transport import RuntimeTransport
    async with RuntimeTransport({"url": "http://probe", "headers": {}}) as transport:
        await transport.client.aclose()
        transport.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500, text="private details")))
        try:
            with pytest.raises(HTTPException) as error:
                await transport.request("/command", {})
            assert error.value.detail == "runtime_http_500"
        finally:
            await transport.client.aclose()

from tests.test_browser_prewarm import runtime
from api.routes.v1.plugin_resources import chat_resources

@pytest.mark.asyncio
async def test_sandbox_fallback_requires_outer_isolation():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from playwright.async_api import Error
    from browser_worker.launch import launch_browser
    launch = AsyncMock(side_effect=[Error("No usable sandbox"), "browser"])
    driver = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    assert await launch_browser(driver, {"chromium_sandbox": True}, container_isolated=True) == "browser"
    assert launch.call_args.kwargs["chromium_sandbox"] is False
    launch.reset_mock(side_effect=True)
    launch.side_effect = Error("No usable sandbox")
    with pytest.raises(ValueError, match="chromium_sandbox_unavailable"):
        await launch_browser(driver, {"chromium_sandbox": True})
    assert launch.await_count == 1

@pytest.mark.asyncio
async def test_screenshot_is_mcp_image(monkeypatch):
    import base64
    from mcp_servers.browser_runtime_mcp import server
    from mcp.types import ImageContent
    async def call(*args):
        return {"data": base64.b64encode(b"png-content").decode()}
    monkeypatch.setattr(server, "call", call)
    result = await server.browser_observe(None, "resource", "screenshot")
    assert isinstance(result.to_image_content(), ImageContent)

@pytest.mark.asyncio
async def test_chat_recovers_only_live_owned_resources(runtime, monkeypatch):
    from api.routes.v1.plugin_resources import chat_resources
    from types import SimpleNamespace
    from core.plugins.resources import service
    db, _, _, _ = runtime
    await service.create(db, "browser", "browser", "warm-user", "warm-chat")
    result = await chat_resources("warm-chat", SimpleNamespace(user_id="warm-user"), db)
    assert len(result["data"]["items"]) == 1
    async def closed(*args):
        return {"closed": True}
    monkeypatch.setattr(service, "request", closed)
    result = await chat_resources("warm-chat", SimpleNamespace(user_id="warm-user"), db)
    assert result["data"]["items"] == []
    with pytest.raises(HTTPException):
        await chat_resources("warm-chat", SimpleNamespace(user_id="other"), db)

@pytest.mark.asyncio
async def test_blur_keeps_control_and_stale_input_reports(browser):
    page = browser.pages[browser.active_tab]
    package = Path(__file__).parents[1] / "plugin_bundles/marketplace/browser-automation/web/browser"
    await page.set_content('<canvas id="screen"></canvas><textarea id="keyboard"></textarea>')
    for script in ("control.js", "touch.js", "input.js"):
        await page.add_script_tag(path=str(package / script))
    await page.evaluate("""() => {
      window.sent = []; window.rejected = 0;
      const state = {controller:'user', connection_id:'viewer', viewport_revision:1, active_tab:'tab'};
      const channel = {connection:()=>'viewer', command:async action=>{sent.push(action); return state;}};
      window.control = installBrowserControl(channel, () => state, () => {}, error => {if(error) rejected++;});
      installBrowserInput(document.getElementById('screen'), document.getElementById('keyboard'), () => state, control);
      window.dispatchEvent(new Event('blur'));
      const keyboard = document.getElementById('keyboard');
      keyboard.value = 'unsent';
      keyboard.dispatchEvent(new Event('input'));
    }""")
    await page.wait_for_timeout(100)
    assert await page.evaluate("sent") == []
    assert await page.evaluate("rejected") == 1
