"""Background loop that serves this computer's Personal Agent mailbox.

Each pass: report local consent, deliver stored receipts, then take at most one screen
command and one session operation. Every action is journaled as started before it runs,
so a command is never executed twice — not after a lost receipt, not after a crash.

Liveness is reported by a heartbeat of its own, so an action that takes a while never
makes the cloud think this computer went away. Every action has a total time limit, and
no error stops the loop: it is logged, reported in the link state, and the loop recovers.

Session operations answer with the shared receipt protocol (hugagent_cli.contract): every
failure says what went wrong and whether it may have taken effect. Up to READ_LANES reads
run at once; a write runs alone, after the reads claimed before it.
"""

import asyncio
import dataclasses
import time
from typing import Optional, Tuple

import httpx

from core.infra.logging import get_logger
from hugagent_cli import contract
from hugagent_cli.contract import validate

from personal_agent.device_link import chat_changes, ACTIONS, SESSIONS, dispatch, local_api, store
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
# A receipt must reach the cloud before the operation's deadline, or it is refused (409)
# and dropped: an operation ends this long before its deadline, and one with less than
# this left does not start.
DEADLINE_MARGIN = 3
# Session reads that may run at the same time. Writes run one at a time, alone.
READ_LANES = 3
# Asking the accessibility host what it can do must never hold up the heartbeat.
CAPABILITIES_TIMEOUT = 3
# After a cloud refused the full heartbeat, the full one is tried again this much later.
LEAN_RETRY = 600
# A mailbox whose passes have all failed for this long is not serving anything, so the
# heartbeat stops claiming the computer is here.
STALLED_AFTER = 30
SEVERITY = ("connected", "connecting", "offline", "error", "unavailable")
TIMED_OUT = {"outcome_unknown": True, "error": "timed_out; check the result before retry"}
# Screen commands only; session operations build receipts with contract.failure.
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
            raise local_api.OperationFailed(
                "account_not_on_this_computer",
                detail="the linked account has not signed in to this desktop app; sign in once and retry")
        return bridge_headers(shadow)


def failure_receipt(operation, exc: BaseException, sent: bool) -> dict:
    """The receipt for an operation that ended in an exception (contract.failure decides its kind)."""
    if isinstance(exc, local_api.OperationFailed):
        return exc.receipt or contract.failure(
            operation, exc.error, sent=sent, http_status=exc.http_status, detail=exc.detail)
    name, detail = type(exc).__name__, str(exc) or None
    if isinstance(exc, httpx.ConnectError):
        # The connection was never made: nothing reached the route.
        return contract.failure(operation, "local_server_unreachable", sent=False, detail=detail, exception=name)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        error = "timed_out"
    elif isinstance(exc, httpx.TransportError):
        error = "connection_lost"
    else:
        error = "local_failure"
    return contract.failure(operation, error, sent=sent, detail=detail, exception=name)


async def bounded(action, seconds: float, what: str, failed=None):
    """Run one journaled action to a result, whatever happens inside it.

    An action that started must end with a receipt: a timeout or an unexpected error is
    reported, never left hanging and never retried here. ``failed(error, exc)`` builds that
    receipt (session operations); without it a screen command reports an unknown outcome.
    """
    try:
        return await asyncio.wait_for(action, seconds)
    except asyncio.TimeoutError as exc:
        if failed:
            return failed("timed_out", exc)
        logger.warning("[device-link] %s timed out after %ss", what, seconds)
        return dict(TIMED_OUT)
    except Exception as exc:
        if failed:
            return failed("local_failure", exc)
        logger.exception("[device-link] %s failed", what)
        return {**FAILED, "exception": type(exc).__name__}


class Mailbox:
    """One linked device of one account."""

    def __init__(self, account: str, link: store.Link, on_done=None, on_change=None):
        self.account = account
        # Told when something the desktop UI shows may have changed (contact, activity, failures).
        self._on_change = on_change or (lambda: None)
        self.link = link
        # Commands and session operations run in lanes of their own: a long session
        # operation never holds a screen command back until its deadline passes. Each lane
        # maps an action id to (task, is_read).
        self.lanes: dict = {"command": {}, "session": {}}
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
        # How much of the heartbeat the cloud accepts: 0 all, 1 no contract/app_version,
        # 2 no capabilities either; and since when it is reduced.
        self.lean, self.lean_since = 0, 0.0
        self.app_version = local_api.app_version()
        # The last session operation that failed, for the desktop UI.
        self.last_failure: Optional[dict] = None
        self.failures = 0
        self._consent_lock = asyncio.Lock()
        self._heartbeat: Optional[asyncio.Task] = None
        self.last_report = 0.0
        self.http = httpx.AsyncClient(
            base_url=f"{link.cloud_base}/api/v1/personal-agent/device-link/{link.device_id}",
            headers={"Authorization": "Bearer " + link.token},
            timeout=REQUEST_TIMEOUT,
        )

    def serves(self, account: str, link: store.Link) -> bool:
        return self.account == account and (self.link.device_id, self.link.token) == (link.device_id, link.token)

    def start(self) -> None:
        # The heartbeat carries this computer's own chat changes to the cloud's open pages.
        chat_changes.install()
        self._heartbeat = asyncio.create_task(self._beat(), name="device-link-heartbeat")

    @property
    def activity(self) -> Optional[dict]:
        """The longest-running action, if any."""
        return min(self.activities.values(), key=lambda a: a["since"], default=None)

    def tasks(self):
        return [task for lane in self.lanes.values() for task, _ in lane.values()]

    def idle(self, kind: str) -> bool:
        return not self.lanes[kind]

    def can_claim(self, kind: str) -> bool:
        """Whether this lane takes new work: a free command lane; reads alongside reads only."""
        lane = self.lanes[kind]
        if kind == "command":
            return not lane
        return len(lane) < READ_LANES and all(read for _, read in lane.values())

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
        for task in self.tasks():
            task.cancel()
        await asyncio.gather(*self.tasks(), return_exceptions=True)
        for lane in self.lanes.values():
            lane.clear()
        if self._heartbeat:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
            self._heartbeat = None
        await self.http.aclose()
        self.journal.close()

    def cloud_now(self) -> float:
        """The cloud's clock, as last measured: deadlines are the cloud's, not this computer's."""
        return time.time() + self.clock_offset

    async def capabilities(self) -> list:
        try:
            return await asyncio.wait_for(dispatch.capabilities(), CAPABILITIES_TIMEOUT)
        except Exception as exc:
            # Without an answer from the accessibility host, report what needs none.
            logger.warning("[device-link] capability check failed: %s", type(exc).__name__)
            return sorted(ACTIONS | {SESSIONS})

    async def report(self, body: dict):
        """POST /heartbeat, dropping what an older cloud refuses (409/422) rather than going offline."""
        if self.lean and time.time() - self.lean_since > LEAN_RETRY:
            self.lean = 0
        shapes = (
            body,
            {k: v for k, v in body.items() if k not in ("contract", "app_version")},
            {k: v for k, v in body.items() if k not in ("contract", "app_version", "capabilities")},
        )
        for level in range(self.lean, len(shapes)):
            try:
                data = await self.device_call("POST", "/heartbeat", shapes[level])
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (409, 422) and level + 1 < len(shapes):
                    logger.warning("[device-link] heartbeat refused with %s; sending less",
                                   exc.response.status_code)
                    continue
                raise
            if level != self.lean:
                self.lean, self.lean_since = level, time.time()
            return data

    async def heartbeat(self) -> None:
        """Tell the cloud this computer is here, with the consent it holds right now.

        Protocol 2 sends both consents in one report and learns the cloud's clock. A cloud
        released before it has no heartbeat route; then restating screen-control consent is
        the liveness report (protocol 1), and session consent is sent only when it changes,
        because an older cloud starts a new session epoch on every "off". A 401/404 makes the
        next heartbeat find out the protocol again.
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
                changes, kept = chat_changes.report_fields(sessions)
                try:
                    # A cloud released before capability reports ignores the field, and one
                    # released before change reports ignores those.
                    data = await self.report({
                        "control_enabled": control, "sessions_enabled": sessions,
                        "capabilities": await self.capabilities(),
                        "contract": contract.contract_version(), "app_version": self.app_version, **changes})
                except DeviceUnavailable:
                    if kept:
                        chat_changes.restore(*kept)
                    if self.protocol == 2:
                        self.protocol = None
                        raise
                    # No such route, or no such device: the legacy route tells the two apart.
                    self.protocol = 1
                except BaseException:
                    # Not delivered: the next report carries these changes again.
                    if kept:
                        chat_changes.restore(*kept)
                    raise
                else:
                    self.protocol = 2
                    self.clock_offset = float(data["server_time"]) - (sent + time.time()) / 2
                    self.reported = {"/consent": control, "/sessions/consent": sessions}
                finally:
                    self.last_report = time.time()
            if self.protocol == 1:
                try:
                    await self.device_call("POST", "/consent", {"enabled": control})
                except DeviceUnavailable:
                    self.protocol = None
                    raise
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
            seen = chat_changes.version()
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
            self._on_change()
            # The next heartbeat is due in HEARTBEAT seconds, or sooner when this computer's chats changed.
            await chat_changes.wait(HEARTBEAT, self.last_report, seen)

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
        """One pass: report consent, deliver receipts, claim work for each lane with room.

        True when something was taken, so the next pass should follow immediately.
        """
        await self.sync_consent()
        await self.flush()
        wanted = [kind for kind in ("command", "session") if self.can_claim(kind)]
        if not wanted:
            self.passed = time.time()
            return False
        try:
            if self.protocol == 2:
                work = await self.device_call("POST", "/claim", {"kinds": wanted}) or {}
                command, operation = work.get("command"), work.get("operation")
            else:
                command = await self.device_call("GET", "/commands") if "command" in wanted else None
                operation = await self.device_call("GET", "/sessions/operations") if "session" in wanted else None
        except DeviceUnavailable:
            self.protocol = None  # Find out the protocol again with the next report.
            raise
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
                elif status in (404, 409):
                    # The cloud already expired or revoked it.
                    logger.warning("[device-link] receipt %s %s refused with %s (expired); dropped", kind, ident, status)
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
        name = operation.get("operation")
        left = operation["deadline"] - self.cloud_now() - DEADLINE_MARGIN
        if left <= 0:
            result = contract.not_executed(
                "expired_before_it_ran", detail="too little time was left before the operation's deadline")
        elif not self.latest().sessions_enabled:
            result = contract.not_executed("permission_revoked", detail="session access is turned off on this computer")
        else:
            self.journal.start("session", ident)
            # Never past the deadline: a receipt that arrives after it is refused and lost.
            seconds = min(OPERATION_TIMEOUT, left)
            attempt = {"sent": False, "budget": seconds}
            read = name in contract.READS
            # A write starts once the reads claimed before it are done.
            before = [] if read else [task for task, _ in self.lanes["session"].values()]

            def failed(error, exc):
                if error == "timed_out":
                    receipt = contract.failure(name, "timed_out", sent=attempt["sent"],
                                               detail=f"no result within {seconds:.0f} s")
                else:
                    receipt = failure_receipt(name, exc, attempt["sent"])
                self.note(name, receipt)
                return receipt

            self._launch("session", ident, self._operate(operation, attempt, before), seconds, failed, read)
            return True
        self.note(name, result)
        self.journal.finish("session", ident, result)
        return True

    def _launch(self, kind: str, ident: str, action, seconds: float, failed=None, read: bool = False) -> None:
        task = asyncio.create_task(self._lane(kind, ident, action, seconds, failed), name=f"device-link-{kind}")
        self.lanes[kind][ident] = (task, read)

    async def _lane(self, kind: str, ident: str, action, seconds: float, failed=None) -> None:
        try:
            result = await self.run(kind, ident, action, seconds, failed)
            self.journal.finish(kind, ident, result)
            try:
                await self.flush()
            except Exception as exc:  # The next pass delivers it.
                logger.warning("[device-link] receipt delivery deferred: %s", type(exc).__name__)
        except Exception:
            # Left "started": the journal reports it unknown when it is reopened.
            logger.exception("[device-link] %s lane failed", kind)
        finally:
            self.lanes[kind].pop(ident, None)
            if self._on_done:
                self._on_done()

    async def run(self, kind: str, ident: str, action, seconds: float, failed=None):
        key = (kind, ident)
        self.activities[key] = {"kind": kind, "id": ident, "since": time.time()}
        self._on_change()
        try:
            return await bounded(action, seconds, f"{kind} {ident}", failed)
        finally:
            self.activities.pop(key, None)
            self._on_change()

    def note(self, operation, receipt) -> None:
        """Count and log a failed session operation, once, without its data."""
        if contract.receipt_status(receipt) in (contract.STATUS_COMPLETED,):
            return
        self.failures += 1
        self.last_failure = {"op": operation, "error": receipt.get("error"), "at": time.time()}
        self._on_change()
        logger.warning(
            "[device-link] session operation %s failed: %s (%s, http_status=%s, exception=%s)",
            operation, receipt.get("error"), contract.receipt_status(receipt),
            receipt.get("http_status"), receipt.get("exception"))

    async def _operate(self, operation, attempt: dict, before):
        if before:
            await asyncio.wait(before)
        return await self.perform(operation, attempt)

    async def perform(self, operation, attempt: Optional[dict] = None):
        """Run one session operation to its receipt. Never raises (except on cancellation)."""
        attempt = {"sent": False} if attempt is None else attempt
        attempt.setdefault("sent", False)
        name = operation.get("operation") if isinstance(operation, dict) else None
        try:
            receipt = await self._perform(operation, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            receipt = failure_receipt(name, exc, attempt["sent"])
            self.note(name, receipt)
            return receipt
        if isinstance(receipt, dict) and (receipt.get("not_executed") or receipt.get("outcome_unknown")):
            self.note(name, receipt)
        return receipt

    async def _perform(self, operation, attempt: dict):
        name, args = operation.get("operation"), operation.get("args")
        try:
            validate(name, args)
        except ValueError as exc:
            # A newer cloud can send an operation this desktop does not know. Refuse; never guess.
            return contract.not_executed("unsupported_operation", detail=f"{exc}; update the desktop app")
        if current_account() != (self.account, self.link.cloud_base):
            return contract.not_executed("account_changed", detail="another account is signed in to this desktop app")
        headers = await asyncio.to_thread(_bridge_headers, self.account)
        try:
            response = await self.http.get("/sessions/operations/" + operation["id"] + "/check")
        except httpx.HTTPError as exc:
            raise local_api.OperationFailed("cloud_check_failed", detail=f"{type(exc).__name__} reaching the cloud") from None
        if response.status_code == 404:
            return contract.not_executed("operation_revoked", http_status=404)
        if not response.is_success:
            return contract.failure(name, "cloud_check_failed", sent=False, http_status=response.status_code,
                                    detail=local_api.server_message(response.content[:4096]) or None)
        # Consent can be withdrawn while the cloud check is in flight.
        if not self.latest().sessions_enabled:
            return contract.not_executed("permission_revoked", detail="session access is turned off on this computer")
        return await local_api.execute(name, args, headers, attempt)


class Runner:
    """Keeps one mailbox served for whichever account is signed in, and never stops on its own."""

    def __init__(self):
        self.state = "unlinked"
        self.error: Optional[str] = None
        self.mailbox: Optional[Mailbox] = None
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        # What the desktop UI was last shown, and a counter its status streams wait on.
        self._shown: Optional[dict] = None
        self.version = 0
        self._next = asyncio.Event()

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
        self.changed()
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

    def changed(self) -> None:
        """Tell open status streams if what the UI shows is different now.

        The last contact time moves on every heartbeat and is not shown, so it alone is no change.
        """
        view = {key: value for key, value in self.snapshot().items() if key != "last_contact"}
        if view == self._shown:
            return
        self._shown = view
        self.version += 1
        self._next.set()
        self._next = asyncio.Event()

    async def watch(self, seen: int, timeout: float) -> int:
        """The status version once it is past ``seen``, or after ``timeout`` seconds without a change."""
        if self.version == seen:
            try:
                await asyncio.wait_for(self._next.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return self.version

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
            # "Connected" alone hides an operation that failed: say which, and how often.
            "last_failure": dict(box.last_failure) if box and box.last_failure else None,
            "failures": box.failures if box else 0,
        }

    async def _pause(self, seconds: float) -> None:
        # Every pass ends here, after it set the state it found.
        self.changed()
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
                        self.mailbox = Mailbox(owner[0], link, on_done=self.wake, on_change=self.changed)
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
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    # A malformed answer from the cloud is as transient as a lost one.
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
