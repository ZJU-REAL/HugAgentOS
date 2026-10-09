"""Fail image builds when the Python driver and offline Chromium disagree."""
import importlib.metadata
import argparse
import os
from pathlib import Path
import fastapi
import uvicorn
from playwright.sync_api import sync_playwright, Error

parser = argparse.ArgumentParser()
parser.add_argument("--container-isolated", action="store_true")
container = parser.parse_args().container_isolated

with sync_playwright() as playwright:
    if not Path(playwright.chromium.executable_path).is_file():
        raise RuntimeError("Python Playwright requires a browser revision absent from this image")
    sandbox = not (container and hasattr(os, "geteuid") and os.geteuid() == 0)
    try:
        browser = playwright.chromium.launch(headless=True, chromium_sandbox=sandbox)
    except Error as error:
        known = any(code in str(error).lower() for code in (
            "no usable sandbox", "suid sandbox", "failed to move to new namespace",
            "running as root without --no-sandbox"))
        if not container or not known:
            raise RuntimeError("Chromium launch failed; inspect runtime and sandbox configuration") from None
        print("Inner sandbox unavailable; using isolated container fallback")
        browser = playwright.chromium.launch(headless=True, chromium_sandbox=False)
    with browser:
        page = browser.new_page()
        page.set_content('<input aria-label="name"><button onclick="document.body.dataset.result=document.querySelector(\'input\').value">Submit</button>')
        page.get_by_role("textbox").fill("离线浏览器")
        page.get_by_role("button").click()
        assert page.locator("body").get_attribute("data-result") == "离线浏览器"
        assert page.screenshot()
        print("Browser runtime verified:", importlib.metadata.version("playwright"), browser.version)
