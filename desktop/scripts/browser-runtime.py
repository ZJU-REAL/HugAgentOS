#!/usr/bin/env python3
"""Install and verify the private Playwright browser shipped in offline runtimes."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from playwright.sync_api import sync_playwright

def check(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "browser-runtime.json").read_text())
    if manifest["playwright"] != importlib.metadata.version("playwright"):
        raise RuntimeError("Browser driver version differs from the bundled manifest")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / manifest["browsers_path"])
    executable = root / manifest["executable"]
    executable.resolve().relative_to(root)
    if hashlib.sha256(executable.read_bytes()).hexdigest() != manifest["executable_sha256"]:
        raise RuntimeError("Bundled browser integrity check failed")
    with sync_playwright() as playwright:
        with playwright.chromium.launch(headless=True, chromium_sandbox=False) as browser:
            page = browser.new_page()
            page.set_content('<input aria-label="name"><button onclick="document.body.dataset.ok=document.querySelector(\'input\').value">Submit</button>')
            page.get_by_role("textbox").fill("离线浏览器")
            page.get_by_role("button").click()
            if page.locator("body").get_attribute("data-ok") != "离线浏览器":
                raise RuntimeError("Bundled browser form smoke test failed")
            if not page.screenshot():
                raise RuntimeError("Bundled browser screenshot smoke test failed")
    return {"playwright": manifest["playwright"], "chromium": manifest["chromium"]}

def install(root):
    root = Path(root).resolve()
    browsers = root / "native/browser"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, timeout=600)
    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)
        with playwright.chromium.launch(headless=True, chromium_sandbox=False) as browser:
            version = browser.version
    manifest = {"schema": 1, "playwright": importlib.metadata.version("playwright"),
        "chromium": version, "browsers_path": "native/browser",
        "executable": executable.relative_to(root).as_posix(),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    (root / "browser-runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
    check(root)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(args.runtime)))
    else:
        install(args.runtime)
