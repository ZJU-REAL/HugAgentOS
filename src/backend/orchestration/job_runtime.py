"""Public job lifecycle API backed by service-owned managed sandbox processes."""

import asyncio
import logging

from core.db.engine import SessionLocal
from core.services.job_service import JobService
from orchestration.jobs import owner, process, state, supervisor

logger = logging.getLogger(__name__)
_launches = {}


async def _launch(jid, user_id, *, resume=False):
    attempt = state.begin_attempt(jid, user_id, resume=resume)
    _launches[jid] = asyncio.current_task()
    try:
        execution = await asyncio.wait_for(process.launch(state.snapshot(jid)), timeout=90)
        entry = supervisor.attach(execution)
        # Confirmation is a runner callback, or a verified terminal receipt.
        await asyncio.wait_for(entry.ready.wait(), timeout=supervisor.STARTUP_SECONDS + 20)
        row = state.snapshot(jid)
        if row["status"] not in ("running", "completed"):
            raise RuntimeError(row["error"] or "作业未能启动")
        return jid
    except Exception as exc:
        row = state.snapshot(jid)
        phase = row["meta"]["execution"].get("phase")
        if phase == "preparing":
            state.save_execution(jid, attempt, phase="not_started")
        elif phase in ("launching", "launched"):
            state.save_execution(jid, attempt, phase="unknown")
        if row["status"] in state.LIVE:
            state.finish(jid, attempt, "failed", f"作业启动失败：{type(exc).__name__}: {exc}")
        raise
    finally:
        _launches.pop(jid, None)


@owner.owned
async def start_job(
    *,
    user_id,
    chat_id,
    name,
    script_path,
    script_text,
    session_id,
    budget=None,
    start_params=None,
    interpreter="${PY_BIN:-python3}",
):
    with SessionLocal() as db:
        job = JobService(db).create(
            user_id=user_id,
            chat_id=chat_id,
            name=name,
            script_path=script_path,
            script_text=script_text,
            sandbox_session_id=session_id,
            budget=budget,
            start_params={**(start_params or {}), "interpreter": interpreter},
        )
        jid = job.job_id
    return await _launch(jid, user_id)


@owner.owned
async def run_and_wait(job_row_id, *, chat_id=None, detach_after=0.0):
    return await supervisor.wait(job_row_id, detach_after)


@owner.owned
async def cancel_job(job_row_id, *, user_id):
    try:
        row = state.snapshot(job_row_id)
    except ValueError:
        return False
    if row["user_id"] != user_id:
        return False
    execution = row["meta"].get("execution") or {}
    attempt = execution.get("attempt_id")
    if not attempt:
        # Legacy nohup jobs have no attributable process handle. Never broad-kill
        # another job or claim to have stopped a process we cannot identify.
        with SessionLocal() as db:
            JobService(db).finish(
                job_row_id, "cancelled", error="旧作业已撤销回调权限，进程状态无法确认"
            )
        raise RuntimeError("旧作业已撤销回调权限，但无持久进程引用，无法确认停止；请勿自动重跑")
    state.finish(job_row_id, attempt, "cancelled", "用户取消", require_owner=False)
    state.take_control(job_row_id, attempt)
    launching = _launches.get(job_row_id)
    if launching:
        await asyncio.gather(asyncio.shield(launching), return_exceptions=True)
        row = state.snapshot(job_row_id)
        execution = row["meta"]["execution"]
    if execution.get("phase") in ("not_started", "exited"):
        return True
    try:
        if not await supervisor.interrupt(job_row_id):
            managed = await process.recover(row)
            if not await managed.stop():
                raise RuntimeError("作业执行权已转移")
    except Exception as exc:
        raise RuntimeError("已撤销作业回调权限，但无法确认原进程停止；禁止重复续跑") from exc
    return True


@owner.owned
async def resume_job(job_row_id, *, user_id, chat_id=None):
    try:
        if state.snapshot(job_row_id)["status"] in state.TERMINAL:
            await supervisor.wait(job_row_id)
        await _launch(job_row_id, user_id, resume=True)
        return {"ok": True, "job_id": job_row_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@owner.owned
async def reap_orphan_jobs():
    count = 0
    for jid in state.claim_expired():
        if jid in supervisor._entries:
            continue
        row = state.snapshot(jid)
        attempt = row["meta"]["execution"]["attempt_id"]
        try:
            managed = await process.recover(row)
            supervisor.attach(managed)
        except Exception as exc:
            phase = row["meta"]["execution"].get("phase")
            if phase == "preparing":
                state.save_execution(jid, attempt, phase="not_started")
            else:
                state.save_execution(jid, attempt, phase="unknown")
            state.finish(
                jid,
                attempt,
                "interrupted",
                "执行实例失联，已撤销旧回调权限；确认原进程退出后才能 resume：" + str(exc),
            )
            from orchestration.jobs.notifications import _maybe_wake

            await _maybe_wake(jid)
        count += 1
    return count


async def resume_running_jobs():
    owner.bind()
    return await reap_orphan_jobs()


async def run_job_reaper_loop():
    owner.bind()
    try:
        while True:
            try:
                await reap_orphan_jobs()
            except Exception as exc:
                logger.warning("Job reconciliation failed: %s", type(exc).__name__)
            await asyncio.sleep(15)
    finally:
        await supervisor.shutdown()
