"""Fail image builds when the Python driver and offline Chromium disagree."""
import importlib.metadata
from pathlib import Path
import fastapi
import uvicorn
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    if not Path(playwright.chromium.executable_path).is_file():
        raise RuntimeError("Python Playwright requires a browser revision absent from this image")
    with playwright.chromium.launch(headless=True, chromium_sandbox=False) as browser:
        page = browser.new_page()
        page.set_content('<input aria-label="name"><button onclick="document.body.dataset.result=document.querySelector(\'input\').value">Submit</button>')
        page.get_by_role("textbox").fill("离线浏览器")
        page.get_by_role("button").click()
        assert page.locator("body").get_attribute("data-result") == "离线浏览器"
        assert page.screenshot()
        print("Browser runtime verified:", importlib.metadata.version("playwright"), browser.version)
