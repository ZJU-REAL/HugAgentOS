"""Selftest: profile compaction scheduling is durable, not event-loop owned.

The old fix used ``run_coroutine_threadsafe`` to get from a DB worker thread
back to the request process's event loop. That repaired thread affinity but a
process exit could still lose the compaction. ``profile._schedule_compact`` now
commits a ``profile_compact`` MemoryOutbox row; the startup worker resumes it.

The executable behavioral coverage lives in:

- ``test_memory_outbox.py`` (restart recovery and idempotent consumption)
- ``test_profile_cas.py`` (compaction conflict reload/recompute)
- ``test_memory_outbox_migration.py`` (SQLite/PostgreSQL schema proof)

Run this structural smoke test directly with
``python3 -m tests.memory.profile_compact_scheduling_selftest``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PROFILE_PY = Path(__file__).resolve().parents[2] / "core" / "memory" / "profile.py"


def _extract_func(source: str, name: str) -> ast.FunctionDef:
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in profile.py")


def _run() -> int:
    source = _PROFILE_PY.read_text(encoding="utf-8")
    schedule = ast.get_source_segment(source, _extract_func(source, "_schedule_compact")) or ""
    assert "enqueue_profile_compaction" in schedule
    assert "create_task" not in schedule
    assert "run_coroutine_threadsafe" not in schedule

    for name in ("_upsert_fields_sync", "_patch_sync"):
        body = ast.get_source_segment(source, _extract_func(source, name)) or ""
        assert "_schedule_compact" in body, f"{name} does not durably enqueue compaction"

    print("profile_compact_scheduling_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
