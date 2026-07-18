"""Selftest: L1 Profile compaction scheduling fix — compact() no longer silently no-ops.

Background
----------
The L1 user profile in `core/memory/profile.py` triggers `compact()` (low-temperature LLM
compaction that trims the profile back under the char limit) when it exceeds
`profile_max_chars` after a write. But the persistence functions `_upsert_fields_sync` /
`_patch_sync` both run via `loop.run_in_executor(None, ...)` on a **thread-pool worker
thread** — worker threads have no running event loop.

The old implementation scheduled compaction from the worker thread with
`asyncio.create_task(compact(ctx))`:

    if over_limit:
        try:
            asyncio.create_task(compact(ctx))      # ← RuntimeError
        except RuntimeError:
            logger.debug("compact scheduled outside event loop, skipped")

`asyncio.create_task` requires a running loop on the **current thread**; worker threads have
none, so it inevitably raises `RuntimeError: no running event loop`, swallowed by
`except RuntimeError`. Consequence: once the L1 profile exceeded `profile_max_chars`,
compaction **never triggered** — the profile grew unboundedly, and the "user profile" frozen
block injected into the system prompt at session start kept ballooning.

Fix
---
Added `_schedule_compact(ctx, loop)`: uses `asyncio.run_coroutine_threadsafe` to post the
coroutine back to the event loop across threads (exactly what that primitive is designed
for). `_upsert_fields_sync` / `_patch_sync` gained a `loop` parameter, with the async caller
passing through the result of `get_running_loop()`.

This test has zero third-party dependencies: it uses `ast` to extract `_schedule_compact`
from profile.py and exec it in an isolated namespace, avoiding pulling in heavy dependencies
like dotenv / sqlalchemy.

Runnable directly with `python3 -m tests.profile_compact_scheduling_selftest`.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

_PROFILE_PY = Path(__file__).resolve().parents[2] / "core" / "memory" / "profile.py"


def _load_profile_source() -> str:
    return _PROFILE_PY.read_text(encoding="utf-8")


def _extract_func(source: str, name: str) -> ast.FunctionDef:
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in profile.py")


class _StubLogger:
    def __init__(self) -> None:
        self.debug_calls: list = []

    def debug(self, *a, **kw) -> None:
        self.debug_calls.append((a, kw))


def _build_schedule_compact(compact_stub, logger_stub):
    """Extract `_schedule_compact` from the source and exec it in an isolated namespace.

    Prepending `from __future__ import annotations` turns the function signature annotations
    into lazy strings, so the namespace needn't provide real types like MemoryContext / Optional.
    """
    source = _load_profile_source()
    func_src = ast.get_source_segment(source, _extract_func(source, "_schedule_compact"))
    code = "from __future__ import annotations\n" + func_src
    ns: dict = {"asyncio": asyncio, "logger": logger_stub, "compact": compact_stub}
    exec(compile(code, "<_schedule_compact>", "exec"), ns)  # noqa: S102
    return ns["_schedule_compact"]


# ---------------------------------------------------------------------------
# Async test cases
# ---------------------------------------------------------------------------

async def _t_create_task_fails_in_worker() -> None:
    """Old-implementation regression pin: asyncio.create_task in a worker thread → RuntimeError.

    This is exactly the root cause the old code's `except RuntimeError` swallowed, so
    compaction never triggered.
    """
    async def _noop() -> int:
        return 1

    def _worker():
        coro = _noop()
        try:
            asyncio.create_task(coro)
            return None
        except RuntimeError as exc:
            coro.close()
            return str(exc)

    loop = asyncio.get_running_loop()
    err = await loop.run_in_executor(None, _worker)
    assert err is not None, "expected RuntimeError from create_task in worker thread"
    assert "running event loop" in err, f"unexpected error: {err!r}"


async def _t_run_coroutine_threadsafe_works_from_worker() -> None:
    """Fix-primitive pin: run_coroutine_threadsafe from a worker thread actually runs the coroutine."""
    ran = asyncio.Event()

    async def _mark() -> None:
        ran.set()

    loop = asyncio.get_running_loop()

    def _worker() -> None:
        asyncio.run_coroutine_threadsafe(_mark(), loop)

    await loop.run_in_executor(None, _worker)
    await asyncio.wait_for(ran.wait(), timeout=2.0)
    assert ran.is_set()


async def _t_schedule_compact_runs_compact() -> None:
    """_schedule_compact posts compact() from a worker thread back to the loop and it really executes."""
    compacted = asyncio.Event()
    seen_ctx: list = []

    async def _compact_stub(ctx) -> bool:
        seen_ctx.append(ctx)
        compacted.set()
        return True

    schedule_compact = _build_schedule_compact(_compact_stub, _StubLogger())
    loop = asyncio.get_running_loop()
    ctx_sentinel = object()

    def _worker() -> None:
        # Same as _upsert_fields_sync / _patch_sync: schedule from a worker thread
        schedule_compact(ctx_sentinel, loop)

    await loop.run_in_executor(None, _worker)
    await asyncio.wait_for(compacted.wait(), timeout=2.0)
    assert compacted.is_set(), "compact() 未被调度执行"
    assert seen_ctx == [ctx_sentinel], "compact() 收到的 ctx 不是透传的那个"


async def _t_schedule_compact_none_loop_noop() -> None:
    """_schedule_compact(ctx, None) safely no-ops: doesn't call compact, doesn't leak a coroutine."""
    called: list = []

    async def _compact_stub(ctx) -> bool:
        called.append(ctx)
        return True

    schedule_compact = _build_schedule_compact(_compact_stub, _StubLogger())
    schedule_compact(object(), None)  # missing loop → return immediately; not even the coroutine is created
    await asyncio.sleep(0)
    assert called == [], "loop=None 时不应调用 compact()"


# ---------------------------------------------------------------------------
# Static structural pins (regression protection)
# ---------------------------------------------------------------------------

def _calls_to(tree: ast.AST, dotted: str) -> int:
    """Count actual calls to `pkg.attr`-form functions in `tree` (ignoring strings/comments)."""
    pkg, attr = dotted.split(".")
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr == attr
                and isinstance(fn.value, ast.Name) and fn.value.id == pkg):
            count += 1
    return count


def _t_source_structure() -> None:
    source = _load_profile_source()
    tree = ast.parse(source)

    # Count actual calls via AST — a mention of `asyncio.create_task` in a docstring doesn't count as a regression
    assert _calls_to(tree, "asyncio.create_task") == 0, (
        "profile.py 仍调用 asyncio.create_task —— 回归到 worker-thread 调度 bug"
    )
    assert _calls_to(tree, "asyncio.run_coroutine_threadsafe") >= 1, (
        "profile.py 未使用 run_coroutine_threadsafe 调度 compact"
    )

    for fn in ("_upsert_fields_sync", "_patch_sync"):
        node = _extract_func(source, fn)
        arg_names = [a.arg for a in node.args.args]
        assert "loop" in arg_names, f"{fn} 缺少 loop 参数"
        body_src = ast.get_source_segment(source, node) or ""
        assert "_schedule_compact" in body_src, f"{fn} 未调用 _schedule_compact"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run() -> int:
    print("=== profile_compact_scheduling_selftest ===")
    failures = 0

    async def _async_suite() -> None:
        nonlocal failures
        cases = [
            ("旧实现 pin: worker 线程内 create_task 抛 RuntimeError（曾被静默吞掉）",
             _t_create_task_fails_in_worker),
            ("修复原语 pin: worker 线程内 run_coroutine_threadsafe 能跑起协程",
             _t_run_coroutine_threadsafe_works_from_worker),
            ("_schedule_compact 从 worker 线程把 compact() 投递回 loop 执行",
             _t_schedule_compact_runs_compact),
            ("_schedule_compact(ctx, None) 安全空跑，不泄漏协程",
             _t_schedule_compact_none_loop_noop),
        ]
        for desc, coro_fn in cases:
            try:
                await coro_fn()
                print(f"  ✓ {desc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ✗ {desc}\n      {exc!r}")

    asyncio.run(_async_suite())

    sync_cases = [
        ("profile.py 不再用 create_task 调度 + 两个 sync writer 透传 loop 调 _schedule_compact",
         _t_source_structure),
    ]
    for desc, fn in sync_cases:
        try:
            fn()
            print(f"  ✓ {desc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ✗ {desc}\n      {exc!r}")

    if failures:
        print(f"=== profile_compact_scheduling_selftest: {failures} FAILED ===")
        return 1
    print("=== profile_compact_scheduling_selftest: OK ===")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_run())
