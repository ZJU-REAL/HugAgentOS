"""Probe the actual Chromium sandbox, with container-only compatibility fallback."""
import logging
from playwright.async_api import Error as PlaywrightError

async def launch_browser(playwright, options, *, container_isolated=False):
    try:
        return await playwright.chromium.launch(**options)
    except PlaywrightError as error:
        message = str(error).lower()
        sandbox_failure = any(code in message for code in (
            "no usable sandbox", "suid sandbox", "failed to move to new namespace", "running as root without --no-sandbox"))
        if sandbox_failure and options.get("chromium_sandbox") and container_isolated:
            logging.getLogger(__name__).warning("Chromium inner sandbox unavailable; using verified outer container isolation")
            try:
                return await playwright.chromium.launch(**{**options, "chromium_sandbox": False})
            except PlaywrightError:
                raise ValueError("chromium_launch_failed: container fallback failed") from None
        if sandbox_failure:
            raise ValueError("chromium_sandbox_unavailable: configure a supported isolated runtime") from error
        if "executable doesn't exist" in message or "browser was not found" in message:
            raise ValueError("chromium_runtime_missing: rebuild the sandbox image with matching Playwright") from error
        raise ValueError("chromium_launch_failed: inspect the sanitized runtime startup log") from error
