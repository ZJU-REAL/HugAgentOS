"""Service-owned execution monitors: handshake, budgets, receipts and cancellation."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import state
from .notifications import _emit_progress, _emit_ui_progress, _maybe_wake, _maybe_wake_progress

logger = logging.getLogger(__name__)
STARTUP_SECONDS = 30.0
PROGRESS_WAKE_SECONDS = 300.0
_entries = {}
_notifications = set()


def _notify(coro):
    async def deliver():
        try:
            await asyncio.wait_for(coro, timeout=120)
        except Exception as exc:
            logger.warning("Job notification failed: %s", type(exc).__name__)

    task = asyncio.create_task(deliver())
    _notifications.add(task)
    task.add_done_callback(_notifications.discard)


@dataclass
class Entry:
    execution: object
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: object = None


def attach(execution):
    jid = execution.job_id
    if jid in _entries:
        raise RuntimeError("Job already has an execution monitor")
    entry = Entry(execution)
    _entries[jid] = entry
    entry.task = asyncio.create_task(_monitor(entry))
    entry.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
    return entry


async def interrupt(jid):
    entry = _entries.get(jid)
    if entry:
        if not await entry.execution.stop():
            raise RuntimeError("作业执行权已转移，无法确认停止")
        return True
    return False


async def wait(jid, timeout=0):
    entry = _entries.get(jid)
    if entry:
        try:
            if timeout > 0:
                return await asyncio.wait_for(asyncio.shield(entry.task), timeout)
            return await asyncio.shield(entry.task)
        except asyncio.TimeoutError:
            return {"status": "detached"}
    row = state.snapshot(jid)
    return {"status": row["status"], "stats": row["stats"], "error": row["error"]}


async def _settle_exit(execution, row, result):
    # A transport/status error is uncertain, not evidence the process exited.
    if result.get("error"):
        state.save_execution(row["job_id"], execution.attempt_id, phase="unknown")
        state.finish(
            row["job_id"],
            execution.attempt_id,
            "interrupted",
            "进程状态查询失败，原执行状态待确认；禁止自动重跑",
        )
        return
    state.save_execution(row["job_id"], execution.attempt_id, phase="exited")
    if row["status"] in state.TERMINAL:
        return
    receipt = await asyncio.wait_for(execution.receipt(), timeout=10)
    if receipt:
        state.finish(
            row["job_id"], execution.attempt_id, receipt["status"], receipt.get("error", "")
        )
    else:
        detail = await asyncio.wait_for(execution.log(), timeout=15)
        state.finish(
            row["job_id"],
            execution.attempt_id,
            "failed",
            f"作业进程退出但未留下完成回执（exit={result.get('exit_code')}）：{detail}",
        )


async def _monitor(entry):
    execution = entry.execution
    jid, attempt = execution.job_id, execution.attempt_id
    first = time.monotonic()
    last_renew = last_ui = 0.0
    last_wake = first
    last_settled = -1
    try:
        while True:
            row = state.snapshot(jid)
            if row["meta"].get("execution", {}).get("attempt_id") != attempt:
                await execution.release()
                return {"status": "interrupted", "error": "执行批次已转移"}
            if row["meta"]["execution"].get("owner") != state.OWNER:
                await execution.release()
                return {"status": "interrupted", "error": "监控租约已转移"}
            if row["status"] == "running":
                entry.ready.set()
            result = await asyncio.wait_for(execution.poll(), timeout=10)
            if result.get("status") == "exited" or result.get("error"):
                await _settle_exit(execution, row, result)
                break
            if row["status"] in state.TERMINAL:
                await execution.stop()
                break
            current = time.monotonic()
            if row["status"] == "pending" and current - first > STARTUP_SECONDS:
                await execution.stop()
                state.finish(
                    jid, attempt, "failed", "作业启动超时：未收到运行回执，已停止该执行实例"
                )
                break
            started = datetime.fromisoformat(row["meta"]["execution"]["started_at"])
            if (state.now() - started).total_seconds() > row["budget"].get("max_seconds", 7200):
                await execution.stop()
                state.finish(jid, attempt, "failed", "超出墙钟预算，已停止作业进程")
                break
            if current - last_renew >= 10:
                if not state.renew(jid, attempt):
                    # Ownership moved after a lease expired. Do not kill the new owner's command.
                    await execution.release()
                    return {"status": "interrupted", "error": "监控租约已转移"}
                last_renew = current
                touch = getattr(execution.provider, "touch_session", None)
                if touch:
                    await asyncio.wait_for(touch(row["session_id"]), timeout=5)
            if current - last_ui >= 5:
                _emit_progress(row["chat_id"], f"作业进行中 done={row['stats']['done']}")
                _emit_ui_progress(row["chat_id"], jid, row["name"], row["stats"])
                last_ui = current
            wake = (
                row["meta"].get("start_params", {}).get("progress_wake_sec", PROGRESS_WAKE_SECONDS)
            )
            if wake > 0 and current - last_wake >= wake:
                last_wake = current
                settled = row["stats"]["settled"]
                _notify(
                    _maybe_wake_progress(
                        jid,
                        stats=row["stats"],
                        budget_left=row["budget_left"],
                        stalled=last_settled >= 0 and settled <= last_settled,
                    )
                )
                last_settled = settled
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        try:
            await execution.stop()
            state.finish(jid, attempt, "interrupted", "服务停止，作业已中断，可显式续跑")
        except Exception:
            state.save_execution(jid, attempt, phase="unknown")
            state.finish(jid, attempt, "interrupted", "服务停止，原进程退出状态待确认")
        raise
    except Exception as exc:
        logger.warning("Job monitor failed job=%s type=%s", jid, type(exc).__name__, exc_info=True)
        try:
            await execution.stop()
            state.finish(jid, attempt, "failed", f"作业监控失败：{type(exc).__name__}")
        except Exception:
            state.save_execution(jid, attempt, phase="unknown")
            state.finish(jid, attempt, "interrupted", "作业监控失联，原执行状态待确认")
    finally:
        entry.ready.set()
        if _entries.get(jid) is entry:
            _entries.pop(jid)
    row = state.snapshot(jid)
    _notify(_maybe_wake(jid))
    return {"status": row["status"], "stats": row["stats"], "error": row["error"]}


async def shutdown():
    tasks = [entry.task for entry in list(_entries.values())]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    notices = list(_notifications)
    for notice in notices:
        notice.cancel()
    await asyncio.gather(*notices, return_exceptions=True)
