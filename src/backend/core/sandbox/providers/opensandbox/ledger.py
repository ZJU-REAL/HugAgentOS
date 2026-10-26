"""Capacity slots for one OpenSandbox controller, shared by every backend worker.

Capacity is a backend policy: ``limit`` fixed slot keys in
:mod:`core.infra.ephemeral` (Redis when configured, in-process for the
single-worker desktop). Each slot is taken with one atomic put-if-absent, so no
global lock exists and over-admission is impossible by construction.

A slot is in exactly one of three states:

- ``creating`` – a live creator renews it; it vanishes with the creator.
- ``bound``    – a sandbox id occupies it; only a confirmed delete or a
  reconciliation that no longer sees the sandbox frees it.
- ``orphan``   – creation failed or was cancelled with an unknown remote
  outcome; reconciliation binds it to the sandbox carrying its token, or frees
  it once the controller can no longer be finishing that request.

Every write keeps the ledger conservative: an uncertain outcome holds a slot
rather than freeing one.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Optional

from core.infra.ephemeral import get_ephemeral_state

# A creator renews its slot well inside this window; a crashed one frees it.
CREATING_TTL_S = 60
# Bound and orphan slots outlive the readiness marker by a wide margin, so a
# ready ledger never has slots expiring underneath it (see ``READY_TTL_S``).
BOUND_TTL_S = 900
# Admission trusts the ledger only while a reconciliation finished this recently.
READY_TTL_S = 300
CREATE_GATE_TTL_S = 60

ADMISSION_TOKEN_KEY = "hugagent-admission-token"


@dataclass(frozen=True)
class Slot:
    state: str
    raw: str
    at: float
    token: Optional[str] = None
    sandbox_id: Optional[str] = None
    grace_s: float = 0.0


def creating_value(token: str) -> str:
    return json.dumps({"s": "creating", "t": token, "at": time.time()})


def orphan_value(token: str, grace_s: float) -> str:
    return json.dumps({"s": "orphan", "t": token, "at": time.time(), "g": grace_s})


def bound_value(sandbox_id: str) -> str:
    return json.dumps({"s": "bound", "id": str(sandbox_id), "at": time.time()})


def parse(raw: Optional[str]) -> Optional[Slot]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return Slot(
            state=str(data["s"]), raw=raw, at=float(data["at"]), token=data.get("t"),
            sandbox_id=data.get("id"), grace_s=float(data.get("g") or 0),
        )
    except (ValueError, KeyError, TypeError):
        # An unreadable slot is still occupied; reconciliation rewrites it.
        return Slot(state="unknown", raw=raw, at=0.0)


class SlotLedger:
    """Atomic slot operations; policy (waiting, priority) lives in admission."""

    def __init__(self, controller: str, limit: int) -> None:
        if limit < 1:
            raise ValueError("sandbox capacity must be positive")
        namespace = hashlib.sha256(controller.encode()).hexdigest()[:16]
        self.prefix = f"jx:sandbox:admission:{namespace}:"
        self.limit = limit

    def slot_key(self, index: int) -> str:
        return f"{self.prefix}slot:{index}"

    def reverse_key(self, sandbox_id: str) -> str:
        return f"{self.prefix}of:{sandbox_id}"

    def gate_key(self, index: int) -> str:
        return f"{self.prefix}gate:{index}"

    @property
    def ready_key(self) -> str:
        return self.prefix + "ready"

    @property
    def overflow_key(self) -> str:
        return self.prefix + "overflow"

    @property
    def reconcile_key(self) -> str:
        return self.prefix + "reconciling"

    @property
    def fresh_key(self) -> str:
        return self.prefix + "fresh"

    @property
    def periodic_key(self) -> str:
        return self.prefix + "periodic"

    async def read(self) -> list[tuple[int, Optional[Slot]]]:
        state = get_ephemeral_state()
        return [(i, parse(await state.get(self.slot_key(i)))) for i in range(self.limit)]

    async def read_all(self) -> list[tuple[int, Optional[Slot]]]:
        """Include slots above ``limit`` left behind by a lowered limit."""
        state = get_ephemeral_state()
        indexes = set(range(self.limit))
        for key in await state.keys(self.prefix + "slot:"):
            try:
                indexes.add(int(key.rsplit(":", 1)[1]))
            except ValueError:
                continue
        return [(i, parse(await state.get(self.slot_key(i)))) for i in sorted(indexes)]

    async def claim(self, index: int, value: str, *, ttl: int) -> bool:
        return await get_ephemeral_state().mutate_if(
            {self.slot_key(index): None}, {self.slot_key(index): value}, ttl=ttl,
        )

    async def renew(self, index: int, raw: str, *, ttl: int) -> bool:
        return await get_ephemeral_state().compare_exchange(
            self.slot_key(index), raw, raw, ttl=ttl,
        )

    async def replace(self, index: int, raw: str, value: str, *, ttl: int) -> bool:
        return await get_ephemeral_state().compare_exchange(
            self.slot_key(index), raw, value, ttl=ttl,
        )

    async def bind(self, index: int, raw: str, sandbox_id: str) -> bool:
        """Turn a creating/orphan slot into the sandbox's slot."""
        return await get_ephemeral_state().mutate_if(
            {self.slot_key(index): raw},
            {self.slot_key(index): bound_value(sandbox_id),
             self.reverse_key(sandbox_id): str(index)},
            ttl=BOUND_TTL_S,
        )

    async def adopt(self, index: int, sandbox_id: str) -> bool:
        """Account a live sandbox nobody holds a slot for."""
        return await get_ephemeral_state().mutate_if(
            {self.slot_key(index): None},
            {self.slot_key(index): bound_value(sandbox_id),
             self.reverse_key(sandbox_id): str(index)},
            ttl=BOUND_TTL_S,
        )

    async def refresh_bound(self, index: int, slot: Slot) -> bool:
        return await get_ephemeral_state().mutate_if(
            {self.slot_key(index): slot.raw},
            {self.slot_key(index): slot.raw, self.reverse_key(slot.sandbox_id): str(index)},
            ttl=BOUND_TTL_S,
        )

    async def move(self, source: int, slot: Slot, target: int) -> bool:
        return await get_ephemeral_state().mutate_if(
            {self.slot_key(source): slot.raw, self.slot_key(target): None},
            {self.slot_key(target): slot.raw, self.reverse_key(slot.sandbox_id): str(target)},
            remove=(self.slot_key(source),),
            ttl=BOUND_TTL_S,
        )

    async def free(self, index: int, slot: Slot) -> bool:
        """Free one observed slot value; never someone else's newer value."""
        state = get_ephemeral_state()
        remove = [self.slot_key(index)]
        expected: dict[str, Optional[str]] = {self.slot_key(index): slot.raw}
        if slot.state == "bound" and slot.sandbox_id:
            reverse = self.reverse_key(slot.sandbox_id)
            if await state.get(reverse) == str(index):
                expected[reverse] = str(index)
                remove.append(reverse)
        return await state.mutate_if(expected, {}, remove=tuple(remove), ttl=BOUND_TTL_S)

    async def locate(self, sandbox_id: str) -> Optional[tuple[int, Slot]]:
        raw_index = await get_ephemeral_state().get(self.reverse_key(sandbox_id))
        if raw_index is not None:
            try:
                index = int(raw_index)
            except ValueError:
                index = -1
            slot = parse(await get_ephemeral_state().get(self.slot_key(index)))
            if slot is not None and slot.sandbox_id == str(sandbox_id):
                return index, slot
        for index, slot in await self.read_all():
            if slot is not None and slot.sandbox_id == str(sandbox_id):
                return index, slot
        return None

    async def overflow(self) -> int:
        raw = await get_ephemeral_state().get(self.overflow_key)
        try:
            return max(0, int(raw or 0))
        except ValueError:
            return 0
