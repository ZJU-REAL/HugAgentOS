"""Tool record/replay cassettes (GCE ticket 15).

Replay only works if it is deterministic, cheap, and free of side effects.  All
three come from one observation: ``tool_call_logs`` already persists both the
arguments and the result of every call.  Keying on ``(tool_name, args_hash)``
turns that history into a cassette — a read-type tool replays its recorded
result and never touches the outside world, so the cost of a counterfactual run
drops from "re-issue every external call" to "pay for tokens only".

Two traps that quietly invalidate everything if ignored:

* Rows flagged ``result_truncated`` hold a clipped result. Replaying the clipped
  form silently changes what the agent saw.
* Long results are offloaded to storage and the row keeps a reference. Replaying
  the reference instead of the content does the same.

Both are backfilled here. A cassette that cannot be faithfully reconstructed
reports a **miss** rather than handing back an approximation — a wrong replay is
worse than no replay, because it produces confident numbers that are not true.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Tools that only read. Anything not listed is treated as potentially mutating
# and is refused during replay — an allow-list, because the dangerous case is
# the write tool nobody remembered to classify.
READ_ONLY_TOOL_PREFIXES: Tuple[str, ...] = (
    "read",
    "glob",
    "grep",
    "search",
    "view",
    "get_",
    "list_",
    "query",
    "fetch",
    "kb_",
    "web_",
)

MISS_NOT_RECORDED = "not_recorded"
MISS_TRUNCATED = "truncated_unrecoverable"
MISS_OFFLOADED = "offloaded_unrecoverable"
MISS_WRITE_TOOL = "write_tool_refused"


def args_hash(tool_args: Any) -> str:
    """Stable hash of call arguments.

    Sorted keys so argument ordering — which the model varies freely between
    runs — never reads as a different call.
    """
    try:
        payload = json.dumps(tool_args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = str(tool_args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def is_read_only(tool_name: str) -> bool:
    name = (tool_name or "").lower()
    return any(name.startswith(prefix) for prefix in READ_ONLY_TOOL_PREFIXES)


@dataclass
class CassetteEntry:
    tool_name: str
    args_hash: str
    result: Any
    status: str = "success"
    duration_ms: Optional[int] = None

    def key(self) -> str:
        return f"{self.tool_name}:{self.args_hash}"


@dataclass
class CassetteMiss:
    tool_name: str
    args_hash: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "reason": self.reason,
        }


@dataclass
class Cassette:
    """Recorded tool interactions for one or more episodes."""

    entries: Dict[str, CassetteEntry] = field(default_factory=dict)
    misses: List[CassetteMiss] = field(default_factory=list)
    # Calls that were skipped at build time because their record could not be
    # faithfully reconstructed. Surfaced so coverage is visible rather than
    # silently degraded.
    unrecoverable: List[CassetteMiss] = field(default_factory=list)

    def add(self, entry: CassetteEntry) -> None:
        self.entries.setdefault(entry.key(), entry)

    def lookup(self, tool_name: str, tool_args: Any) -> Tuple[Optional[CassetteEntry], str]:
        """Find a recorded result. Returns ``(entry, miss_reason)``."""
        digest = args_hash(tool_args)
        if not is_read_only(tool_name):
            # A write tool must never execute during replay, and it has no
            # meaningful recorded result to hand back either.
            miss = CassetteMiss(tool_name, digest, MISS_WRITE_TOOL)
            self.misses.append(miss)
            return None, MISS_WRITE_TOOL

        entry = self.entries.get(f"{tool_name}:{digest}")
        if entry is None:
            miss = CassetteMiss(tool_name, digest, MISS_NOT_RECORDED)
            self.misses.append(miss)
            return None, MISS_NOT_RECORDED
        return entry, ""

    @property
    def coverage(self) -> float:
        """Fraction of lookups that were served from the recording."""
        total = len(self.entries) + len(self.misses)
        return len(self.entries) / total if total else 0.0

    def report(self) -> Dict[str, Any]:
        return {
            "entries": len(self.entries),
            "misses": len(self.misses),
            "unrecoverable": [m.to_dict() for m in self.unrecoverable],
            "coverage": round(self.coverage, 4),
        }


def _recover_result(row: Any) -> Tuple[Any, Optional[str]]:
    """Reconstruct the full result a run actually saw.

    Returns ``(result, failure_reason)``. A row whose content cannot be restored
    yields a reason instead of a best-effort approximation.
    """
    result = getattr(row, "tool_result", None)

    if getattr(row, "result_truncated", False):
        # The stored value is clipped. If the full text is not recoverable from
        # the offload store, replaying the clipped form would change what the
        # agent saw, so the entry is dropped rather than faked.
        restored = _load_offloaded(result)
        if restored is None:
            return None, MISS_TRUNCATED
        return restored, None

    reference = _offload_reference(result)
    if reference is not None:
        restored = _load_offloaded(result)
        if restored is None:
            return None, MISS_OFFLOADED
        return restored, None

    return result, None


def _offload_reference(result: Any) -> Optional[str]:
    """Detect a stored pointer rather than inline content."""
    if isinstance(result, dict):
        for key in ("_offload_ref", "offload_ref", "payload_ref", "artifact_ref"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _load_offloaded(result: Any) -> Optional[Any]:
    """Fetch offloaded content. Returns ``None`` when it cannot be restored."""
    reference = _offload_reference(result)
    if reference is None:
        return None
    try:
        from core.llm.offloader import load_offloaded_result  # type: ignore

        restored = load_offloaded_result(reference)
        return restored if restored is not None else None
    except Exception as exc:
        logger.debug("[cassette] offload restore failed for %s: %s", reference, exc)
        return None


def build_cassette(message_ids: Sequence[str]) -> Cassette:
    """Assemble a cassette from recorded tool calls for the given runs."""
    cassette = Cassette()
    if not message_ids:
        return cassette

    try:
        from core.db.engine import SessionLocal
        from core.db.models import ToolCallLog

        with SessionLocal() as db:
            rows = (
                db.query(ToolCallLog)
                .filter(ToolCallLog.message_id.in_(list(message_ids)))
                .all()
            )
            for row in rows:
                tool_name = row.tool_name or ""
                if not is_read_only(tool_name):
                    # Write tools are deliberately not recorded for replay: they
                    # will be refused, not replayed.
                    continue
                result, failure = _recover_result(row)
                digest = args_hash(row.tool_args)
                if failure:
                    cassette.unrecoverable.append(
                        CassetteMiss(tool_name, digest, failure)
                    )
                    continue
                cassette.add(
                    CassetteEntry(
                        tool_name=tool_name,
                        args_hash=digest,
                        result=result,
                        status=row.status or "success",
                        duration_ms=row.duration_ms,
                    )
                )
    except Exception as exc:
        logger.warning("[cassette] build failed: %s", exc)
    return cassette


class ReplayToolGate:
    """Intercepts tool calls during replay.

    Read tools are served from the cassette; write tools are refused outright
    rather than executed in dry-run mode, because a "dry run" of an unknown tool
    is an assumption, not a guarantee. Refusing keeps the invariant
    checkable: **a replay performs no external writes, ever.**
    """

    def __init__(self, cassette: Cassette):
        self.cassette = cassette
        self.refused_writes: List[str] = []
        self.served: int = 0

    def call(self, tool_name: str, tool_args: Any) -> Tuple[bool, Any, str]:
        """Returns ``(served, result, reason)``."""
        if not is_read_only(tool_name):
            self.refused_writes.append(tool_name)
            return False, None, MISS_WRITE_TOOL

        entry, miss = self.cassette.lookup(tool_name, tool_args)
        if entry is None:
            return False, None, miss
        self.served += 1
        return True, entry.result, ""

    def report(self) -> Dict[str, Any]:
        return {
            "served": self.served,
            "refused_writes": sorted(set(self.refused_writes)),
            "cassette": self.cassette.report(),
        }
