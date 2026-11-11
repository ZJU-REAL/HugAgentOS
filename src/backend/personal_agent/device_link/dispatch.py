"""Where one screen command runs on this computer, and how long it may take.

``read`` and ``steps`` go to the macOS accessibility host while it is usable; everything
else, and every command on other systems, goes to the screenshot driver, which runs as a
child process per command. The single actions older clouds send (``observe``, ``click``…)
always use the screenshot driver: their coordinates are screen pixels.
"""

import asyncio
import json
import sys
from pathlib import Path

from core.infra.logging import get_logger
from core.infra.proc import no_window_kwargs

from personal_agent.device_link import ACTIONS, SESSIONS, UI_ELEMENTS
from personal_agent.device_link import ui_host
from personal_agent.device_link.driver import COMMAND_SECONDS

logger = get_logger(__name__)

DRIVER = Path(__file__).with_name("driver.py")
DRIVER_TIMEOUT = 20
# Total time one command may take on this computer: the cloud's own limit for read and steps
# (driver.COMMAND_SECONDS), a little over the driver's own limit for a single old action.
COMMAND_TIMEOUT = DRIVER_TIMEOUT + 5
# An action that may have reached the screen before it failed: never retried here.
FAILED = {"outcome_unknown": True, "error": "local_failure; check the result before retry"}


def time_limit(action: dict) -> float:
    return COMMAND_SECONDS.get(action.get("type"), COMMAND_TIMEOUT)


async def run_driver(action, timeout: float = DRIVER_TIMEOUT) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(DRIVER), json.dumps(action),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **no_window_kwargs(),
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        return {"outcome_unknown": True}
    finally:
        # Also on cancellation: a driver must never outlive the action that started it.
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        return {"error": "Local desktop driver failed"}
    try:
        return json.loads(output)
    except ValueError:
        return {"outcome_unknown": True}


async def execute(action: dict) -> dict:
    """Run one screen command to its receipt."""
    kind, host = action.get("type"), ui_host.host
    if kind in COMMAND_SECONDS and await host.available():
        try:
            return await getattr(host, kind)(action)
        except Exception:
            logger.exception("[device-link] accessibility %s failed", kind)
            if kind == "read":
                return {"error": "read_failed; read again"}
            # Some steps may have reached the screen before the failure.
            return dict(FAILED)
    # The child process gets a little less than the lane's limit, so its own timeout, not
    # the lane's, ends a stuck driver: 10 s for a read, 70 s for a batch, 20 s otherwise.
    result = await run_driver(action, time_limit(action) - 5)
    if kind == "read" and host.reason not in (None, "not_macos") and "mode" in result and not result.get("degraded_reason"):
        # Say why a Mac answers with pixels: the skill can tell the user what to allow.
        result["degraded_reason"] = host.reason
    return result


async def capabilities() -> list:
    """Contract A.5: what this computer reports with each heartbeat."""
    elements = await ui_host.host.available()
    return sorted(ACTIONS | {SESSIONS} | ({UI_ELEMENTS} if elements else set()))
