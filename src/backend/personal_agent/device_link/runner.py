"""Background loop that serves this computer's Personal Agent mailbox.

Each pass: report local consent, deliver stored receipts, then take at most one screen
command and one session operation. Every action is journaled as started before it runs,
so a command is never executed twice — not after a lost receipt, not after a crash.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx

from core.infra.logging import get_logger
from core.infra.proc import no_window_kwargs
from hugagent_cli.contract import validate

from personal_agent.device_link import ACTIONS, local_api, store

logger = get_logger(__name__)

DRIVER = Path(__file__).with_name("driver.py")
DRIVER_TIMEOUT = 20
ACTIVE_POLL = 0.5
IDLE_POLL = 2
UNLINKED_POLL = 5
OFFLINE_RETRY = 5
MAX_BACKOFF = 300


class DeviceUnavailable(Exception):
    """The cloud no longer accepts this device's credential (revoked or unknown)."""


def current_account() -> Optional[Tuple[str, str]]:
    """``(account, cloud_base)`` of the cloud identity the desktop shell signed in with."""
    from features.desktop.bridge.identity import get_identity_state

    identity = get_identity_state()
    return (identity["shell_user_center_id"], identity["cloud_base"]) if identity else None


def current_link(owner: Optional[Tuple[str, str]]) -> Optional[store.Link]:
    """The link of the signed-in account, if it belongs to the cloud the desktop uses now."""
    link = store.load(owner[0]) if owner else None
    return link if link and link.cloud_base == owner[1] else None


async def run_driver(action) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(DRIVER), json.dumps(action),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **no_window_kwargs(),
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), DRIVER_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {"outcome_unknown": True}
    if process.returncode != 0:
        return {"error": "Local desktop driver failed"}
    try:
        return json.loads(output)
    except ValueError:
        return {"outcome_unknown": True}


def _bridge_headers(account: str) -> dict:
    from features.desktop.auth.bridge import bridge_headers
    from core.db.engine import SessionLocal
    from core.db.models import UserShadow

    with SessionLocal() as db:
        shadow = db.query(UserShadow).filter(UserShadow.user_center_id == account).one_or_none()
        if shadow is None:
            raise ValueError("Linked account is not present on this computer")
        return bridge_headers(shadow)


class Mailbox:
    """One linked device of one account."""

    def __init__(self, account: str, link: store.Link):
        self.account = account
        self.link = link
        self.journal = store.Journal(account)
        self.reported: dict = {}
        self.http = httpx.AsyncClient(
            base_url=f"{link.cloud_base}/api/v1/personal-agent/device-link/{link.device_id}",
            headers={"Authorization": "Bearer " + link.token},
            timeout=15,
        )

    def serves(self, account: str, link: store.Link) -> bool:
        return self.account == account and (self.link.device_id, self.link.token) == (link.device_id, link.token)

    async def close(self) -> None:
        await self.http.aclose()
        self.journal.close()

    async def device_call(self, method: str, path: str, body=None):
        response = await self.http.request(method, path, json=body)
        if response.status_code in (401, 404):
            raise DeviceUnavailable()
        response.raise_for_status()
        return response.json()["data"]

    async def step(self) -> bool:
        """One pass. True when an action ran, so the next pass should follow immediately."""
        for path, enabled in (("/consent", self.link.control_enabled), ("/sessions/consent", self.link.sessions_enabled)):
            if self.reported.get(path) != enabled:
                await self.device_call("POST", path, {"enabled": enabled})
                self.reported[path] = enabled
        await self.flush()
        busy = False
        command = await self.device_call("GET", "/commands")
        if command:
            busy = await self.handle_command(command)
        operation = await self.device_call("GET", "/sessions/operations")
        if operation:
            busy = await self.handle_operation(operation) or busy
        return busy

    async def flush(self) -> None:
        for kind, ident, result in self.journal.pending():
            path = ("/commands/" if kind == "command" else "/sessions/operations/") + ident
            response = await self.http.post(path, json={"result": result})
            # 404/409: the cloud already expired or revoked it. The receipt is no longer deliverable.
            if not (response.is_success or response.status_code in (404, 409)):
                response.raise_for_status()
            self.journal.close_receipt(kind, ident)

    async def handle_command(self, command) -> bool:
        ident = command["id"]
        if self.journal.seen("command", ident) or command["deadline"] <= time.time():
            return False
        action = command["action"]
        if not self.link.control_enabled:
            result = {"waiting": True, "message": "Control is turned off on this computer"}
        elif action.get("type") not in ACTIONS:
            result = {"error": "Capability disabled locally", "not_executed": True}
        else:
            self.journal.start("command", ident)
            result = await run_driver(action)
        self.journal.finish("command", ident, result)
        await self.flush()
        return True

    async def handle_operation(self, operation) -> bool:
        ident = operation["id"]
        if self.journal.seen("session", ident) or operation["deadline"] <= time.time() or not self.link.sessions_enabled:
            return False
        self.journal.start("session", ident)
        result = await self.perform(operation)
        self.journal.finish("session", ident, result)
        await self.flush()
        return True

    async def perform(self, operation):
        try:
            validate(operation["operation"], operation["args"])
        except ValueError:
            # A newer cloud can send an operation this desktop does not know. Refuse; never guess.
            return {"error": "unsupported_operation; update the desktop app", "not_executed": True}
        try:
            if current_account() != (self.account, self.link.cloud_base):
                return {"error": "account_changed", "not_executed": True}
            headers = await asyncio.to_thread(_bridge_headers, self.account)
            response = await self.http.get("/sessions/operations/" + operation["id"] + "/check")
            if response.status_code == 404:
                return {"error": "operation_revoked", "not_executed": True}
            response.raise_for_status()
            # Consent can be withdrawn while the cloud check is in flight.
            latest = store.load(self.account)
            if not (latest and latest.device_id == self.link.device_id and latest.sessions_enabled):
                return {"error": "permission_revoked", "not_executed": True}
            return await local_api.execute(operation["operation"], operation["args"], headers)
        except (httpx.HTTPError, RuntimeError, ValueError):
            return {"outcome_unknown": True, "error": "local_operation_failed; check local session before retry"}


class Runner:
    def __init__(self):
        self.state = "unlinked"
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="device-link")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def wake(self) -> None:
        """Pick up a changed link or consent now instead of after the current pause."""
        self._wake.set()

    async def _pause(self, seconds: float) -> None:
        if seconds > 0:
            try:
                await asyncio.wait_for(self._wake.wait(), seconds)
            except asyncio.TimeoutError:
                pass
        self._wake.clear()

    async def _loop(self) -> None:
        mailbox: Optional[Mailbox] = None
        backoff = 0
        try:
            while True:
                owner = current_account()
                link = current_link(owner)
                if mailbox and not (link and mailbox.serves(owner[0], link)):
                    await mailbox.close()
                    mailbox, backoff = None, 0
                if link is None:
                    self.state = "unlinked"
                    await self._pause(UNLINKED_POLL)
                    continue
                if mailbox is None:
                    mailbox = Mailbox(owner[0], link)
                mailbox.link = link
                try:
                    busy = await mailbox.step()
                    self.state, backoff = "connected", 0
                    consented = link.control_enabled or link.sessions_enabled
                    await self._pause(0 if busy else ACTIVE_POLL if consented else IDLE_POLL)
                except DeviceUnavailable:
                    self.state = "unavailable"
                    backoff = min(max(backoff * 2, OFFLINE_RETRY), MAX_BACKOFF)
                    await self._pause(backoff)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    self.state = "offline"
                    logger.warning("[device-link] cloud unreachable: %s", type(exc).__name__)
                    await self._pause(OFFLINE_RETRY)
        finally:
            if mailbox:
                await mailbox.close()


runner = Runner()
