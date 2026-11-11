"""Background loop that serves this computer's Personal Agent mailbox.

Each pass: report local consent, deliver stored receipts, then take at most one screen
command and one session operation. Every action is journaled as started before it runs,
so a command is never executed twice — not after a lost receipt, not after a crash.

Liveness is reported by a heartbeat of its own, so an action that takes a while never
makes the cloud think this computer went away. Every action has a total time limit, and
no error stops the loop: it is logged, reported in the link state, and the loop recovers.
"""

import asyncio
import dataclasses
import time
from typing import Optional, Tuple

import httpx

from core.infra.logging import get_logger
from hugagent_cli.contract import validate

from personal_agent.device_link import ACTIONS, dispatch, local_api, store
from personal_agent.device_link.ui_host import host as ui_host

logger = get_logger(__name__)

ACTIVE_POLL = 0.5
IDLE_POLL = 2
UNLINKED_POLL = 5
OFFLINE_RETRY = 5
MAX_BACKOFF = 300
# The cloud counts a device offline 20 s after its last report. A heartbeat every 5 s with
# requests capped at 10 s keeps one slow or lost request from crossing that line.
HEARTBEAT = 5
REQUEST_TIMEOUT = 10
# Total time one session operation may take. It expires in the cloud after 60 s, and its
# receipt is refused after that, so waiting longer only delays the next one. Screen
# commands have limits by type (dispatch.time_limit).
OPERATION_TIMEOUT = 60
# A mailbox whose passes have all failed for this long is not serving anything, so the
# heartbeat stops claiming the computer is here.
STALLED_AFTER = 30
SEVERITY = ("connected", "connecting", "offline", "error", "unavailable")
TIMED_OUT = {"outcome_unknown": True, "error": "timed_out; check the result before retry"}
FAILED = dispatch.FAILED


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


def _bridge_headers(account: str) -> dict:
    from features.desktop.auth.bridge import bridge_headers
    from core.db.engine import SessionLocal
    from core.db.models import UserShadow

    with SessionLocal() as db:
        shadow = db.query(UserShadow).filter(UserShadow.user_center_id == account).one_or_none()
        if shadow is None:
            raise ValueError("Linked account is not present on this computer")
        return bridge_headers(shadow)


async def bounded(action, seconds: float, what: str):
    """Run one journaled action to a result, whatever happens inside it.

    An action that started must end with a receipt: a timeout or an unexpected error is
    reported as an unknown outcome, never left hanging and never retried here.
    """
    try:
        return await asyncio.wait_for(action, seconds)
    except asyncio.TimeoutError:
        logger.warning("[device-link] %s timed out after %ss", what, seconds)
        return dict(TIMED_OUT)
    except Exception:
        logger.exception("[device-link] %s failed", what)
        return dict(FAILED)


class Mailbox:
    """One linked device of one account."""

    def __init__(self, account: str, link: store.Link, on_done=None):
        self.account = account
        self.link = link
        # Commands and session operations run in lanes of their own: a long session
        # operation never holds a screen command back until its deadline passes.
        self.lanes: dict = {"command": None, "session": None}
        self.activities: dict = {}
        self.passed = time.time()
        self._on_done = on_done
        self._flush_lock = asyncio.Lock()
        self.journal = store.Journal(account)
        self.reported: dict = {}
        # What the heartbeat last learned: connecting, connected, offline or unavailable.
        self.contact = "connecting"
        self.last_contact: Optional[float] = None
        # 2: heartbeat and claim routes; 1: a cloud released before them; None: not known yet.
        self.protocol: Optional[int] = None
        self.clock_offset = 0.0
        self._consent_lock = asyncio.Lock()
        self._heartbeat: Optional[asyncio.Task] = None
        self.http = httpx.AsyncClient(
            base_url=f"{link.cloud_base}/api/v1/personal-agent/device-link/{link.device_id}",
            headers={"Authorization": "Bearer " + link.token},
            timeout=REQUEST_TIMEOUT,
        )

    def serves(self, account: str, link: store.Link) -> bool:
        return self.account == account and (self.link.device_id, self.link.token) == (link.device_id, link.token)

    def start(self) -> None:
        self._heartbeat = asyncio.create_task(self._beat(), name="device-link-heartbeat")

    @property
    def activity(self) -> Optional[dict]:
        """The longest-running action, if any."""
        return min(self.activities.values(), key=lambda a: a["since"], default=None)

    def idle(self, kind: str) -> bool:
        task = self.lanes[kind]
        return task is None or task.done()

    def latest(self) -> store.Link:
        """This computer's consent as it is on disk now, not as it was at the start of a pass.

        A permission the user withdrew a moment ago must already count. A link that was
        removed or replaced grants nothing.
        """
        latest = store.load(self.account)
        if latest and latest.device_id == self.link.device_id:
            return latest
        return dataclasses.replace(self.link, control_enabled=False, sessions_enabled=False)

    async def close(self) -> None:
        # An action cut off here stays "started" in the journal: reopening it reports the
        # action as an unknown outcome, never runs it again.
        for kind, task in self.lanes.items():
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.lanes[kind] = None
        if self._heartbeat:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
            self._heartbeat = None
        await self.http.aclose()
        self.journal.close()

    def cloud_now(self) -> float:
        """The cloud's clock, as last measured: deadlines are the cloud's, not this computer's."""
        return time.time() + self.clock_offset

    async def heartbeat(self) -> None:
        """Tell the cloud this computer is here, with the consent it holds right now.

        Protocol 2 sends both consents in one report and learns the cloud's clock. A cloud
        released before it has no heartbeat route; then restating screen-control consent is
        the liveness report (protocol 1), and session consent is sent only when it changes,
        because an older cloud starts a new session epoch on every "off".
        """
        if time.time() - self.passed > STALLED_AFTER and all(map(self.idle, self.lanes)):
            # Every pass has failed for a while: the cloud must not route work here.
            self.contact = "offline"
            return
        async with self._consent_lock:
            link = self.latest()
            control, sessions = link.control_enabled, link.sessions_enabled
            if self.protocol != 1:
                sent = time.time()
                try:
                    # A cloud released before capability reports ignores the field.
                    data = await self.device_call("POST", "/heartbeat", {
                        "control_enabled": control, "sessions_enabled": sessions,
                        "capabilities": await dispatch.capabilities()})
                except DeviceUnavailable:
                    if self.protocol == 2:
                        raise
                    # No such route, or no such device: the legacy route tells the two apart.
                    self.protocol = 1
                else:
                    self.protocol = 2
                    self.clock_offset = float(data["server_time"]) - (sent + time.time()) / 2
                    self.reported = {"/consent": control, "/sessions/consent": sessions}
            if self.protocol == 1:
                await self.device_call("POST", "/consent", {"enabled": control})
                self.reported["/consent"] = control
                if self.reported.get("/sessions/consent") != sessions:
                    try:
                        await self.device_call("POST", "/sessions/consent", {"enabled": sessions})
                    except httpx.HTTPStatusError as exc:
                        # Paired before session support: there is no session consent to report.
                        if exc.response.status_code != 409:
                            raise
                    self.reported["/sessions/consent"] = sessions
        self.contact, self.last_contact = "connected", time.time()

    async def _beat(self) -> None:
        while True:
            try:
                await self.heartbeat()
            except DeviceUnavailable:
                self.contact = "unavailable"
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                if self.contact != "offline":
                    logger.warning("[device-link] heartbeat failed: %s", type(exc).__name__)
                self.contact = "offline"
            except Exception:
                logger.exception("[device-link] heartbeat failed unexpectedly")
                self.contact = "offline"
            await asyncio.sleep(HEARTBEAT)

    async def sync_consent(self) -> None:
        """Report a consent changed on this computer now, not at the next heartbeat."""
        link = self.latest()
        wanted = {"/consent": link.control_enabled, "/sessions/consent": link.sessions_enabled}
        if self.protocol is None or self.reported != wanted:
            await self.heartbeat()

    async def device_call(self, method: str, path: str, body=None):
        response = await self.http.request(method, path, json=body)
        if response.status_code in (401, 404):
            raise DeviceUnavailable()
        response.raise_for_status()
        return response.json()["data"]

    async def step(self) -> bool:
        """One pass: report consent, deliver receipts, claim work for each idle lane.

        True when something was taken, so the next pass should follow immediately.
        """
        await self.sync_consent()
        await self.flush()
        wanted = [kind for kind in ("command", "session") if self.idle(kind)]
        if not wanted:
            self.passed = time.time()
            return False
        if self.protocol == 2:
            work = await self.device_call("POST", "/claim", {"kinds": wanted}) or {}
            command, operation = work.get("command"), work.get("operation")
        else:
            command = await self.device_call("GET", "/commands") if "command" in wanted else None
            operation = await self.device_call("GET", "/sessions/operations") if "session" in wanted else None
        # Only a pass that could take work counts as serving.
        self.passed = time.time()
        taken = False
        if command:
            taken = self.accept_command(command) or taken
        if operation:
            taken = self.accept_operation(operation) or taken
        return taken

    async def flush(self) -> None:
        async with self._flush_lock:
            for kind, ident, result in self.journal.pending():
                path = ("/commands/" if kind == "command" else "/sessions/operations/") + ident
                response = await self.http.post(path, json={"result": result})
                status = response.status_code
                if not (response.is_success or status in (404, 409)):
                    if status >= 500 or status in (408, 429):
                        response.raise_for_status()  # Worth another try on the next pass.
                    # Refused for good (e.g. 403/413 from a proxy). Holding it would block
                    # every later receipt, so it is dropped; the cloud reports it unknown.
                    logger.warning("[device-link] receipt %s %s refused with %s; dropped", kind, ident, status)
                # 404/409: the cloud already expired or revoked it.
                self.journal.close_receipt(kind, ident)

    def accept_command(self, command) -> bool:
        ident = command["id"]
        if self.journal.seen("command", ident):
            return False
        action = command["action"]
        if command["deadline"] <= self.cloud_now():
            result = {"error": "expired before it ran", "not_executed": True}
        elif not self.latest().control_enabled:
            result = {"waiting": True, "message": "Control is turned off on this computer", "not_executed": True}
        elif action.get("type") not in ACTIONS:
            result = {"error": "Capability disabled locally", "not_executed": True}
        else:
            self.journal.start("command", ident)
            self._launch("command", ident, self._command(action), dispatch.time_limit(action))
            return True
        self.journal.finish("command", ident, result)
        return True

    async def _command(self, action):
        # Consent is read once more right before the input reaches the screen.
        if not self.latest().control_enabled:
            return {"waiting": True, "message": "Control is turned off on this computer", "not_executed": True}
        return await dispatch.execute(action)

    def accept_operation(self, operation) -> bool:
        ident = operation["id"]
        if self.journal.seen("session", ident):
            return False
        if operation["deadline"] <= self.cloud_now():
            result = {"error": "expired before it ran", "not_executed": True}
        elif not self.latest().sessions_enabled:
            result = {"error": "permission_revoked", "not_executed": True}
        else:
            self.journal.start("session", ident)
            seconds = min(OPERATION_TIMEOUT, max(5, operation["deadline"] - self.cloud_now()))
            self._launch("session", ident, self.perform(operation), seconds)
            return True
        self.journal.finish("session", ident, result)
        return True

    def _launch(self, kind: str, ident: str, action, seconds: float) -> None:
        self.lanes[kind] = asyncio.create_task(self._lane(kind, ident, action, seconds), name=f"device-link-{kind}")

    async def _lane(self, kind: str, ident: str, action, seconds: float) -> None:
        try:
            result = await self.run(kind, ident, action, seconds)
            self.journal.finish(kind, ident, result)
            try:
                await self.flush()
            except Exception as exc:  # The next pass delivers it.
                logger.warning("[device-link] receipt delivery deferred: %s", type(exc).__name__)
        except Exception:
            # Left "started": the journal reports it unknown when it is reopened.
            logger.exception("[device-link] %s lane failed", kind)
        finally:
            if self._on_done:
                self._on_done()

    async def run(self, kind: str, ident: str, action, seconds: float):
        self.activities[kind] = {"kind": kind, "id": ident, "since": time.time()}
        try:
            return await bounded(action, seconds, f"{kind} {ident}")
        finally:
            self.activities.pop(kind, None)

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
            if not self.latest().sessions_enabled:
                return {"error": "permission_revoked", "not_executed": True}
            return await local_api.execute(operation["operation"], operation["args"], headers)
        except (httpx.HTTPError, RuntimeError, ValueError):
            return {"outcome_unknown": True, "error": "local_operation_failed; check local session before retry"}


class Runner:
    """Keeps one mailbox served for whichever account is signed in, and never stops on its own."""

    def __init__(self):
        self.state = "unlinked"
        self.error: Optional[str] = None
        self.mailbox: Optional[Mailbox] = None
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="device-link")
        self._task.add_done_callback(self._supervise)

    def _supervise(self, task: asyncio.Task) -> None:
        # The loop handles its own errors; this only catches what slipped past it.
        if self._stopping or task.cancelled():
            return
        exc = task.exception()
        logger.error("[device-link] loop ended unexpectedly: %r; restarting", exc)
        self.state, self.error = "error", type(exc).__name__ if exc else "stopped"
        asyncio.get_running_loop().call_later(OFFLINE_RETRY, self._restart)

    def _restart(self) -> None:
        if not self._stopping:
            self.start()

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await ui_host.close()

    def wake(self) -> None:
        """Pick up a changed link or consent now instead of after the current pause."""
        self._wake.set()

    def snapshot(self) -> dict:
        """What the desktop UI shows about this computer's link."""
        box = self.mailbox
        state = self.state
        if box and state != "unlinked":
            # The heartbeat and the mailbox pass each see the cloud; show the worse of the two.
            state = max(state, box.contact, key=SEVERITY.index)
        return {
            "state": state,
            "error": self.error if state == "error" else None,
            "last_contact": box.last_contact if box else None,
            "activity": dict(box.activity) if box and box.activity else None,
        }

    async def _pause(self, seconds: float) -> None:
        if seconds > 0:
            try:
                await asyncio.wait_for(self._wake.wait(), seconds)
            except asyncio.TimeoutError:
                pass
        self._wake.clear()

    async def _drop(self) -> None:
        if self.mailbox:
            box, self.mailbox = self.mailbox, None
            await box.close()

    async def _loop(self) -> None:
        backoff = 0
        try:
            while True:
                try:
                    owner = current_account()
                    link = current_link(owner)
                    if self.mailbox and not (link and self.mailbox.serves(owner[0], link)):
                        await self._drop()
                        backoff = 0
                    if link is None:
                        self.state, self.error = "unlinked", None
                        await self._pause(UNLINKED_POLL)
                        continue
                    if self.mailbox is None:
                        self.state = "connecting"
                        self.mailbox = Mailbox(owner[0], link, on_done=self.wake)
                        self.mailbox.start()
                    self.mailbox.link = link
                    busy = await self.mailbox.step()
                    self.state, self.error, backoff = "connected", None, 0
                    consented = link.control_enabled or link.sessions_enabled
                    await self._pause(0 if busy else ACTIVE_POLL if consented else IDLE_POLL)
                except DeviceUnavailable:
                    self.state = "unavailable"
                    backoff = min(max(backoff * 2, OFFLINE_RETRY), MAX_BACKOFF)
                    await self._pause(backoff)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    if self.state != "offline":
                        logger.warning("[device-link] cloud unreachable: %s", type(exc).__name__)
                    self.state = "offline"
                    await self._pause(OFFLINE_RETRY)
                except Exception as exc:
                    # Anything else is a defect, not a network condition. Start over with a
                    # fresh mailbox: reopening the journal reports any interrupted action as
                    # an unknown outcome instead of leaving it started forever.
                    logger.exception("[device-link] mailbox pass failed")
                    self.state, self.error = "error", type(exc).__name__
                    await self._drop()
                    await self._pause(OFFLINE_RETRY)
        finally:
            await self._drop()


runner = Runner()
