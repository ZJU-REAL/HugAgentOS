"""Memory's named settlement boundary; callers retain their transaction scope."""
from __future__ import annotations
from typing import Any, Protocol


class MemorySettlement(Protocol):
    def report_writes(self, message_id: str, *, items: list[dict], failed: bool) -> None: ...
    def update_entry_text(self, message_id: str, handle: str, text: str) -> bool: ...
    def build_summary(self, message_id: str, *, session_factory: Any, items: list[dict], failed: bool) -> dict[str, Any]: ...
    def write_summary(self, db: Any, message_id: str, summary: Any) -> None: ...





def register_memory_settlement(service: MemorySettlement) -> None:
    process_state().service = service


def memory_settlement() -> MemorySettlement:
    if process_state().service is None:
        raise RuntimeError("Memory settlement service is not registered")
    return process_state().service


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache

@state_dataclass
class SettlementPortState:
    """Process-owned storage; domain operations retain locks and cleanup responsibility."""
    service: MemorySettlement | None = None

@state_cache(maxsize=1)
def process_state() -> SettlementPortState:
    return SettlementPortState()

# Initialize under the import lock before concurrent callers share the owner.
process_state()
