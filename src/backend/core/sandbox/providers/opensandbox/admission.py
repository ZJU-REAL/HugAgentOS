"""The single way to create an OpenSandbox instance.

Every ``Sandbox.create`` against a controller goes through
:meth:`SandboxAdmission.create` (a guard test enforces it). Admission takes a
capacity slot from :mod:`ledger`, then one of a few creation gates, and only
then calls the controller. The controller performs Docker operations only.

Why this shape:

- Waiting happens here, on the event loop, with a deadline. A request that loses
  a race for a free slot simply takes another one; it is never refused because
  someone else is busy creating.
- Interactive requests wait up to ``OPENSANDBOX_ADMISSION_WAIT_S``. Pool
  pre-warm runs under :func:`background` and never waits nor dips into the
  slots reserved for users.
- The ledger is trusted only right after a reconciliation against the
  controller's inventory. Reconciliation frees slots of vanished sandboxes,
  accounts sandboxes nobody holds a slot for, and resolves orphans. It runs
  before the first admission, periodically, and — completed after the request
  started — before admission reports exhaustion, so a leaked slot can delay a
  user but never fail one.
- Waiters poll with jittered backoff; there is no FIFO order. Under sustained
  saturation an early request can time out while a later one is admitted.
- Releasing on delete is an optimisation. Correctness never depends on every
  kill path calling :meth:`SandboxAdmission.release`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
import time
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Iterator, Optional

from core.infra.ephemeral import get_ephemeral_state
from core.sandbox.errors import SandboxCapacityExhausted, SandboxError
from core.sandbox.providers.opensandbox.ledger import (
    ADMISSION_TOKEN_KEY,
    BOUND_TTL_S,
    CREATE_GATE_TTL_S,
    CREATING_TTL_S,
    READY_TTL_S,
    SlotLedger,
    creating_value,
    orphan_value,
    parse,
)

logger = logging.getLogger(__name__)

_POLL_MIN_S = 0.05
_POLL_MAX_S = 1.0
# Waiters on a full ledger force a reconciliation at most this often.
FORCE_MIN_INTERVAL_S = 5
PERIODIC_INTERVAL_S = 60
RECONCILE_LOCK_TTL_S = 120
RECONCILE_WAIT_S = 60
# Inventory and slot timestamps come from different backend clocks.
CLOCK_SKEW_S = 30

_BACKGROUND: ContextVar[bool] = ContextVar("sandbox_admission_background", default=False)

Launch = Callable[[dict[str, str]], Awaitable[Any]]


@contextlib.contextmanager
def background() -> Iterator[None]:
    """Mark creations in this context as pre-warm: no waiting, no user headroom."""
    token = _BACKGROUND.set(True)
    try:
        yield
    finally:
        _BACKGROUND.reset(token)


class _Lease:
    """Keep a creating slot and its gate alive while the controller works."""

    def __init__(self, admission: "SandboxAdmission", index: int, raw: str) -> None:
        self._admission = admission
        self.index = index
        self.raw = raw
        self.gate: Optional[tuple[int, str]] = None
        self._task = asyncio.create_task(self._renew())

    async def _renew(self) -> None:
        ledger = self._admission.ledger
        state = get_ephemeral_state()
        while True:
            await asyncio.sleep(CREATING_TTL_S / 4)
            try:
                # A lapsed slot is handled when binding; keep renewing the gate.
                await ledger.renew(self.index, self.raw, ttl=CREATING_TTL_S)
                if self.gate is not None:
                    await state.hold(ledger.gate_key(self.gate[0]), self.gate[1],
                                     ttl=CREATE_GATE_TTL_S)
            except Exception as exc:  # noqa: BLE001 — the next beat retries
                logger.warning("[sandbox-admission] lease renewal failed: %s", exc)

    async def stop(self) -> None:
        self._task.cancel()
        # ``wait`` instead of awaiting the task: suppressing CancelledError there
        # would also swallow a cancellation aimed at the caller.
        await asyncio.wait([self._task])
        if self.gate is not None:
            index, token = self.gate
            self.gate = None
            with contextlib.suppress(Exception):
                await get_ephemeral_state().release(
                    self._admission.ledger.gate_key(index), token)


class SandboxAdmission:
    def __init__(
        self,
        *,
        controller: str,
        limit: int,
        config_factory: Callable[[], Any],
        create_concurrency: int,
        wait_s: float,
        headroom: int,
        ambiguous_grace_s: float,
    ) -> None:
        self.ledger = SlotLedger(controller, limit)
        self.controller = controller
        self._config_factory = config_factory
        self._gates = max(1, create_concurrency)
        self._wait_s = max(0.0, wait_s)
        self._headroom = min(max(0, headroom), limit - 1)
        self._ambiguous_grace_s = ambiguous_grace_s

    @property
    def limit(self) -> int:
        return self.ledger.limit

    # ───── creation ───────────────────────────────────────────────────────

    async def create(self, launch: Launch, *, metadata: Optional[dict[str, str]] = None) -> Any:
        """Reserve capacity, then run ``launch(metadata)`` and account its sandbox."""
        is_background = _BACKGROUND.get()
        started = time.monotonic()
        deadline = started + (0.0 if is_background else self._wait_s)
        token = uuid.uuid4().hex
        index, raw = await self._reserve(background=is_background, deadline=deadline, token=token,
                                         requested_at=time.time())
        lease = _Lease(self, index, raw)
        launched = False
        try:
            lease.gate = await self._enter_gate(
                token, background=is_background,
                deadline=deadline if is_background
                else max(deadline, time.monotonic() + self._wait_s),
            )
            waited = time.monotonic() - started
            if waited > 1:
                logger.info("[sandbox-admission] admitted after %.1fs (slot %d)", waited, index)
            launched = True
            sandbox = await launch({**(metadata or {}), ADMISSION_TOKEN_KEY: token})
        except BaseException as exc:
            await lease.stop()
            await asyncio.shield(self._settle_failure(lease, token, exc, launched=launched))
            raise
        await lease.stop()
        await asyncio.shield(self._bind(lease, str(sandbox.id)))
        return sandbox

    async def _reserve(self, *, background: bool, deadline: float, token: str,
                       requested_at: float) -> tuple[int, str]:
        await self._ensure_ready()
        floor = self._headroom if background else 0
        delay = _POLL_MIN_S
        while True:
            slots = await self.ledger.read()
            free = [index for index, slot in slots if slot is None]
            if len(free) - await self.ledger.overflow() > floor:
                random.shuffle(free)
                for index in free:
                    value = creating_value(token)
                    if await self.ledger.claim(index, value, ttl=CREATING_TTL_S):
                        return index, value
                continue  # Raced for every free slot; read again.
            if not background:
                # Leaked or vanished slots must be reclaimed before anyone is
                # told the deployment is full: require a reconciliation that
                # finished after this request began, then at most one per interval.
                try:
                    await self.reconcile(fresh_after=max(
                        requested_at, time.time() - FORCE_MIN_INTERVAL_S))
                except Exception as exc:  # noqa: BLE001 — keep waiting on the ledger
                    logger.warning("[sandbox-admission] reconciliation while waiting failed: %s",
                                   exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Exhaustion needs a completed pass; an unreachable controller
                # only extends the wait by one reconciliation budget.
                if (not background and time.monotonic() < deadline + RECONCILE_WAIT_S
                        and not await self._reconciled_since(requested_at)):
                    await asyncio.sleep(_POLL_MAX_S)
                    continue
                raise SandboxCapacityExhausted(limit=self.limit, background=background)
            await asyncio.sleep(min(delay, remaining) * random.uniform(0.5, 1.0))
            delay = min(delay * 2, _POLL_MAX_S)

    async def _reconciled_since(self, moment: float) -> bool:
        raw = await get_ephemeral_state().get(self.ledger.fresh_key)
        try:
            return raw is not None and float(raw) >= moment
        except ValueError:
            return False

    async def _enter_gate(self, token: str, *, background: bool,
                          deadline: float) -> tuple[int, str]:
        """Bound concurrent controller creations; gates turn over in seconds."""
        state = get_ephemeral_state()
        delay = _POLL_MIN_S
        while True:
            for index in random.sample(range(self._gates), self._gates):
                if await state.hold(self.ledger.gate_key(index), token, ttl=CREATE_GATE_TTL_S):
                    return index, token
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxCapacityExhausted(limit=self.limit, background=background,
                                               queue_full=True)
            await asyncio.sleep(min(delay, remaining) * random.uniform(0.5, 1.0))
            delay = min(delay * 2, _POLL_MAX_S)

    async def _settle_failure(self, lease: _Lease, token: str, exc: BaseException,
                              *, launched: bool) -> None:
        try:
            outcome = _failure_outcome(exc) if launched else "unsent"
            if outcome == "unsent":
                await self.ledger.free(lease.index, parse(lease.raw))
                return
            # A sandbox carrying this token may exist (a readiness failure whose
            # cleanup failed, or a controller still working after the client gave
            # up). Hold the slot until reconciliation sees the outcome: at once
            # when the controller answered, after the request timeout otherwise.
            grace = 0.0 if outcome == "answered" else self._ambiguous_grace_s
            await self.ledger.replace(lease.index, lease.raw, orphan_value(token, grace),
                                      ttl=BOUND_TTL_S)
        except Exception as settle_error:  # noqa: BLE001 — reconciliation repairs it
            logger.warning("[sandbox-admission] failed to settle slot %d: %s",
                           lease.index, settle_error)

    async def _bind(self, lease: _Lease, sandbox_id: str) -> None:
        try:
            if await self.ledger.bind(lease.index, lease.raw, sandbox_id):
                return
            # The creating lease lapsed (a stalled store). Account the sandbox in
            # any free slot, or make the next admission reconcile first.
            for index, slot in await self.ledger.read():
                if slot is None and await self.ledger.adopt(index, sandbox_id):
                    return
            logger.warning("[sandbox-admission] sandbox %s admitted without a slot; "
                           "forcing reconciliation", sandbox_id)
            await get_ephemeral_state().drop(self.ledger.ready_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sandbox-admission] bind %s failed: %s", sandbox_id, exc)
            with contextlib.suppress(Exception):
                await get_ephemeral_state().drop(self.ledger.ready_key)

    async def release(self, sandbox_id: str) -> None:
        """Free a sandbox's slot after the controller confirmed its deletion."""
        try:
            found = await self.ledger.locate(str(sandbox_id))
            if found is not None:
                await self.ledger.free(*found)
        except Exception as exc:  # noqa: BLE001 — reconciliation frees it later
            logger.warning("[sandbox-admission] release %s deferred: %s", sandbox_id, exc)

    # ───── reconciliation ─────────────────────────────────────────────────

    async def _ensure_ready(self) -> None:
        state = get_ephemeral_state()
        deadline = time.monotonic() + RECONCILE_WAIT_S
        while not await state.get(self.ledger.ready_key):
            if time.monotonic() >= deadline:
                raise SandboxError("沙箱容量账本暂时无法与控制器对账，请稍后重试")
            await self.reconcile(deadline=deadline)

    async def reconcile_if_due(self) -> None:
        if await get_ephemeral_state().claim(self.ledger.periodic_key, ttl=PERIODIC_INTERVAL_S):
            await self.reconcile()

    async def reconcile(self, *, deadline: Optional[float] = None,
                        fresh_after: Optional[float] = None) -> None:
        """Make the ledger match the controller; concurrent callers share one pass.

        With ``fresh_after``, a pass that completed at or after that moment counts.
        """
        state = get_ephemeral_state()
        if fresh_after is not None and await self._reconciled_since(fresh_after):
            return
        deadline = deadline or time.monotonic() + RECONCILE_WAIT_S
        token = uuid.uuid4().hex
        delay = _POLL_MIN_S
        while not await state.hold(self.ledger.reconcile_key, token, ttl=RECONCILE_LOCK_TTL_S):
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_S)
            if not await state.get(self.ledger.reconcile_key):
                return  # Another worker just finished; its result is as fresh.
        try:
            await asyncio.wait_for(self._reconcile_locked(), timeout=RECONCILE_LOCK_TTL_S - 10)
        finally:
            with contextlib.suppress(Exception):
                await state.release(self.ledger.reconcile_key, token)

    async def _reconcile_locked(self) -> None:
        ledger = self.ledger
        started = time.time()
        inventory = await self._inventory()
        by_token = {tok: sid for sid, tok in inventory.items() if tok}
        slots = await ledger.read_all()
        accounted: set[str] = set()
        freed = adopted = 0
        for index, slot in slots:
            if slot is None:
                continue
            if slot.state == "bound":
                if slot.sandbox_id in inventory:
                    if await ledger.refresh_bound(index, slot):
                        accounted.add(slot.sandbox_id)
                elif slot.at < started - CLOCK_SKEW_S and await ledger.free(index, slot):
                    freed += 1
                # A slot bound after the inventory snapshot stays as it is.
            elif slot.state == "creating":
                if slot.token in by_token:
                    accounted.add(by_token[slot.token])  # Its creator binds it next.
            elif slot.state == "orphan":
                sandbox_id = by_token.get(slot.token)
                if sandbox_id is not None:
                    if await ledger.bind(index, slot.raw, sandbox_id):
                        accounted.add(sandbox_id)
                elif slot.at + slot.grace_s < started and await ledger.free(index, slot):
                    freed += 1
        slots = await ledger.read_all()
        free = [index for index, slot in slots if slot is None and index < ledger.limit]
        # A lowered limit leaves bound slots above it; pull them into range.
        for index, slot in slots:
            if index < ledger.limit or slot is None or slot.state != "bound":
                continue
            while free:
                if await ledger.move(index, slot, free.pop(0)):
                    break
        held = {slot.sandbox_id for _, slot in slots if slot is not None and slot.state == "bound"}
        overflow = 0
        for sandbox_id in inventory:
            if sandbox_id in accounted or sandbox_id in held:
                continue
            while free:
                if await ledger.adopt(free.pop(0), sandbox_id):
                    adopted += 1
                    break
            else:
                # Live but unaccountable: admission subtracts these from free slots.
                overflow += 1
        state = get_ephemeral_state()
        if overflow:
            await state.put(ledger.overflow_key, str(overflow), ttl=READY_TTL_S)
        else:
            await state.drop(ledger.overflow_key)
        await state.put(ledger.ready_key, "1", ttl=READY_TTL_S)
        await state.put(ledger.fresh_key, repr(time.time()), ttl=READY_TTL_S)
        if freed or adopted or overflow:
            logger.info(
                "[sandbox-admission] reconciled %s: inventory=%d freed=%d adopted=%d overflow=%d",
                self.controller, len(inventory), freed, adopted, overflow,
            )

    async def _inventory(self) -> dict[str, Optional[str]]:
        """Every sandbox the controller lists, any state, with its admission token."""
        from opensandbox import SandboxManager
        from opensandbox.models.sandboxes import SandboxFilter

        manager = await SandboxManager.create(self._config_factory())
        try:
            inventory: dict[str, Optional[str]] = {}
            page = 1
            while True:
                result = await manager.list_sandbox_infos(SandboxFilter(page=page, page_size=200))
                for info in result.sandbox_infos or []:
                    inventory[str(info.id)] = (info.metadata or {}).get(ADMISSION_TOKEN_KEY)
                pagination = getattr(result, "pagination", None)
                if not result.sandbox_infos or not getattr(pagination, "has_next_page", False):
                    return inventory
                page += 1
                if page > 50:
                    raise SandboxError("controller inventory exceeded the pagination safety limit")
        finally:
            with contextlib.suppress(Exception):
                await manager.close()


def _failure_outcome(exc: BaseException) -> str:
    """``unsent``: never reached the controller; ``answered``: it replied with an
    HTTP status; ``unknown``: anything else, including cancellation and timeouts."""
    if isinstance(exc, asyncio.CancelledError):
        return "unknown"
    import httpx

    cause: Optional[BaseException] = exc
    while cause is not None:
        if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout)):
            return "unsent"
        cause = cause.__cause__ or cause.__context__
    # The SDK reports transport failures with status_code 0.
    status = getattr(exc, "status_code", None)
    return "answered" if isinstance(status, int) and status > 0 else "unknown"


_ADMISSIONS: dict[tuple[str, int], SandboxAdmission] = {}
_ADMISSIONS_LOCK = threading.Lock()


def admission_for(controller: str, *, limit: int,
                  config_factory: Callable[[], Any]) -> SandboxAdmission:
    """One admission per controller; workers coordinate through the shared ledger."""
    from core.config.settings import settings

    s = settings.sandbox
    key = (controller.rstrip("/"), int(limit))
    with _ADMISSIONS_LOCK:
        admission = _ADMISSIONS.get(key)
        if admission is None:
            admission = SandboxAdmission(
                controller=key[0], limit=key[1], config_factory=config_factory,
                create_concurrency=s.opensandbox_create_concurrency,
                wait_s=s.opensandbox_admission_wait_s,
                headroom=s.opensandbox_user_headroom,
                # Readiness runs after the create call returns and the SDK kills
                # what it created on failure, so only the call itself is unknown.
                ambiguous_grace_s=s.opensandbox_request_timeout_s + 30,
            )
            _ADMISSIONS[key] = admission
        return admission


def controller_domain() -> str:
    from core.config.settings import settings

    domain = settings.sandbox.opensandbox_domain.rstrip("/")
    return domain if domain.startswith(("http://", "https://")) else "http://" + domain


def connection_config() -> Any:
    from datetime import timedelta

    from opensandbox.config import ConnectionConfig
    from core.config.settings import settings

    s = settings.sandbox
    return ConnectionConfig(
        domain=controller_domain(), api_key=s.opensandbox_api_key or None,
        use_server_proxy=True,
        request_timeout=timedelta(seconds=s.opensandbox_request_timeout_s),
    )


def opensandbox_admission() -> SandboxAdmission:
    """Admission for the deployment's sandbox controller (chat, pools, evaluation)."""
    from core.config.settings import settings

    return admission_for(
        controller_domain(),
        limit=max(1, settings.sandbox.opensandbox_pool_max_total),
        config_factory=connection_config,
    )


__all__ = [
    "SandboxAdmission", "admission_for", "background", "connection_config", "controller_domain",
    "opensandbox_admission",
]
