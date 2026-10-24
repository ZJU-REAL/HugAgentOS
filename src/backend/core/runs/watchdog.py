"""Detect stalled streams and reap expired runs without replacing a live writer."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _datetime
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Optional

import core.db.engine as engine
import core.db.models as models
import core.llm.runtime.human_interaction as human_interaction
import core.runs.clock as clock
import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.settings as settings
import core.runs.tasks as tasks
from core.infra.logging import get_logger
from core.db.threads import run_in_session

logger = get_logger(__name__)


async def _aiter_with_inactivity_timeout(
    aiter: AsyncIterator[Any],
    timeout: float,
    *,
    is_activity: Callable[[Any], bool] | None = None,
    first_response_timeout: float | None = None,
):
    """Yield from an async iterator and enforce a meaningful-activity deadline.

    Guards against a hung workflow (model loop / blocked MCP / asyncio
    deadlock) leaving the chat run stuck in 'running' forever — on timeout
    the underlying generator is closed and the error propagates into the
    existing ``except Exception`` path that writes the ``failed`` terminal.

    Transport keep-alives may still be yielded to the caller, but they do not
    reset the deadline when ``is_activity`` marks them as non-meaningful.
    The optional first-response deadline is absolute until visible output;
    it is then disabled for the remainder of this workflow.
    """
    activity_check = is_activity or (lambda _: True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    first_deadline = (
        loop.time() + first_response_timeout if first_response_timeout is not None else None
    )

    def timeout_error() -> TimeoutError:
        from core.infra.exceptions import FirstResponseTimeoutError

        if first_deadline is not None and loop.time() >= first_deadline:
            return FirstResponseTimeoutError()
        return TimeoutError(f"工作流 {timeout:.0f}s 内无有效输出，已判定为卡死并中止")

    while True:
        effective_deadline = (
            min(deadline, first_deadline) if first_deadline is not None else deadline
        )
        remaining = effective_deadline - loop.time()
        if remaining <= 0:
            with contextlib.suppress(Exception):
                await aiter.aclose()  # type: ignore[attr-defined]
            raise timeout_error()
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                await aiter.aclose()  # type: ignore[attr-defined]
            raise timeout_error() from exc
        if first_deadline is not None and _is_first_response(item):
            first_deadline = None
        if activity_check(item):
            deadline = loop.time() + timeout
        yield item


def _is_first_response(item: Any) -> bool:
    """Only visible model output or a tool interaction ends the initial wait."""
    kind = item.get("type")
    if kind in {"ai_message", "content", "thinking"}:
        return bool(item.get("delta"))
    if kind == "content_replace":
        return bool(item.get("content"))
    return kind in {
        "tool_call_start",
        "tool_call_delta",
        "tool_call",
        "tool_result",
        "tool_pending",
        "file_confirm",
        "plan_update",
        "compaction_progress",
    }


def _is_chat_run_activity(item: Any, chat_id: str) -> bool:
    """Classify workflow output for the inactivity watchdog.

    A transport heartbeat is normally not meaningful progress. It becomes
    progress only while a registered tool is legitimately suspended on a
    human answer; this keeps true HITL waits alive without weakening zombie
    detection for every other silent run.
    """

    return item.get("type") != "heartbeat" or human_interaction.has_pending(chat_id)


async def _stream_last_write_ms(run_id: str) -> Optional[int]:
    """Write time of the last event on the run's log (epoch ms).

    Cursors are minted as ``<ms>-<seq>`` by every backend, so the millisecond
    half is the write time — no extra bookkeeping. Returns None when there is
    no log / no events / the read fails (callers treat that as "no activity").
    """
    try:
        return await writer._events().last_write_ms(run_id)
    except Exception as exc:
        logger.warning("chat_run_activity_check_failed", run_id=run_id, error=str(exc))
        return None


def _chat_has_live_job(chat_id: Optional[str], now: datetime) -> bool:
    """这个会话是否挂着**还在推进**的批量作业（跨进程可见的活性证据）。

    判据是 ``jobs.updated_at`` 在静默窗口内前进过：作业每完成一次子调用都会写 usage，
    onupdate 随之推进 updated_at。只要作业还在动，这个 run 就不是僵尸——哪怕它的
    asyncio task 属于另一个进程（多 worker / 多副本），也哪怕主链路一个流事件都没有。

    作业本身有 max_seconds 墙钟预算与熔断，所以这条豁免不会让 run 永生。
    """
    if not chat_id:
        return False
    try:
        from core.db.models import Job

        cutoff = now - _datetime.timedelta(seconds=settings._STALE_QUIET_SEC)
        with engine.SessionLocal() as db:
            return (
                db.query(Job.job_id)
                .filter(
                    Job.chat_id == chat_id,
                    Job.status.in_(("pending", "running")),
                    Job.updated_at >= cutoff,
                )
                .first()
                is not None
            )
    except Exception:  # noqa: BLE001 —— 证据查不到就按原判据走，绝不因此放过真僵尸
        return False


async def reap_stale_runs() -> int:
    """Periodic safety net: fail 'running' runs that show no sign of life.

    The verdict is activity-aware, in two tiers (both tunable via env vars):

    - Reap only when **over-age + quiet**: lifetime exceeds
      ``CHAT_RUN_MAX_AGE_SEC`` AND the Redis Stream's last write is older than
      ``CHAT_RUN_STALE_QUIET_SEC``. Long tasks still steadily producing
      tool_call/content are no longer falsely killed (historical bug: purely
      age-based reaping choked even active runs mid-tool-call once the 30-minute
      mark hit).
    - **Absolute cap**: runs older than ``CHAT_RUN_HARD_MAX_AGE_SEC`` are reaped
      even when actively emitting, except for automation conversations and
      registered bounded human interactions (which keep their own deadline).

    Reaping also cancels the in-process worker task, keeping the DB terminal
    state aligned with the actual task (historical bug: only flipping the DB
    without killing the task let the worker silently run another 1.5h and
    overwrite the terminal state back to completed). plan_execute is the
    exception — cross-task cancel risks the anyio cancel-scope deadlock (see the
    ``cancel_run`` docstring); it stops itself after ``is_run_cancelled``
    polling observes the terminal state.

    Complements periodic ``recover_orphan_runs`` and the per-run
    inactivity watchdog — also sweeps up historical zombie runs left by
    older code paths.
    """
    from core.db.models import ChatSession
    from sqlalchemy import func

    now = clock._utcnow()
    cutoff = now - _datetime.timedelta(seconds=settings._STALE_RUN_MAX_AGE_SEC)
    hard_cutoff = now - _datetime.timedelta(seconds=settings._HARD_MAX_AGE_SEC)
    def _stale_candidates(db):
        candidates = (
            db.query(
                models.ChatRun.run_id,
                models.ChatRun.chat_id,
                models.ChatRun.request_payload,
                models.ChatRun.lease_expires_at,
                ChatSession.extra_data.label("session_metadata"),
                func.coalesce(models.ChatRun.started_at, models.ChatRun.created_at).label(
                    "began_at"
                ),
            )
            .outerjoin(ChatSession, ChatSession.chat_id == models.ChatRun.chat_id)
            .filter(models.ChatRun.status == "running")
            .filter(func.coalesce(models.ChatRun.started_at, models.ChatRun.created_at) < cutoff)
            .all()
        )
        return candidates
    candidates = await run_in_session(_stale_candidates)
    if not candidates:
        return 0

    now_ms = int(now.timestamp() * 1000)
    reaped = 0
    for row in candidates:
        rid, cid = row.run_id, row.chat_id
        lease_expires_at = row.lease_expires_at
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=_datetime.timezone.utc)
        began_at = row.began_at
        if began_at is not None and began_at.tzinfo is None:  # SQLite stores naive UTC
            began_at = began_at.replace(tzinfo=_datetime.timezone.utc)
        session_metadata = row.session_metadata or {}
        automation_run = bool(session_metadata.get("automation_run"))
        hard_expired = not automation_run and began_at is not None and began_at < hard_cutoff
        if not hard_expired and lease_expires_at is not None and lease_expires_at > now:
            logger.info(
                "chat_run_stale_skip_live_lease",
                run_id=rid,
                lease_expires_at=lease_expires_at.isoformat(),
            )
            continue
        payload = row.request_payload if isinstance(row.request_payload, dict) else {}

        # 自主循环豁免统一年龄硬顶：loop 的使命就是「跑到任务完成为止」（去预算化），
        # 且它有自己的停滞护栏/熔断/硬后备轮数。进程内 task 还活着的 loop 绝不按
        # 年龄杀（历史竞态：6h 硬顶 == loop 旧默认预算，跑满预算的健康 loop 在优雅
        # 收尾前被硬顶抢杀，报错还误导为「无响应」）。孤儿 loop（进程重启遗留）
        # 走下面的静默判据正常清理。
        if hard_expired and payload.get("kind") == "autonomous_loop":
            task = tasks.process_state().active_runs.get(rid)
            if task is not None and not task.done():
                logger.info("chat_run_stale_skip_loop_inprocess", run_id=rid)
                continue
            hard_expired = False  # 孤儿 loop：不按年龄硬杀，交给静默判据

        # A legitimate user decision may begin near the run's absolute-age
        # ceiling. Its own bounded wait deadline is the authority; do not cut a
        # newly opened question/confirmation down to the remainder of the run
        # hard cap. Once the registry resolves or times out, this exemption
        # disappears automatically on the next sweep.
        if hard_expired and human_interaction.has_pending(cid):
            logger.info("chat_run_stale_skip_human_wait", run_id=rid, chat_id=cid)
            continue

        # 工作流模式的批量作业：run 卡在 run_job(wait=True) 里等作业，期间主链路一个
        # 流事件都不产生（逐项结果按设计不进对话），"流静默"判据必然误判。进程内 task
        # 豁免只在本进程有效——多 worker / 多副本部署里，另一个进程看不到这个 task，
        # 照样会把健康的长作业杀掉。所以这里用**跨进程可见的证据**：DB 里这个会话是否
        # 还有正在推进的作业（每次子调用都会 add_usage → jobs.updated_at 前进）。
        if _chat_has_live_job(cid, now):
            logger.info("chat_run_stale_skip_live_job", run_id=rid, chat_id=cid)
            continue

        if not hard_expired:
            # A run whose asyncio task is alive in THIS process is never a
            # zombie — the in-process inactivity watchdog owns hang detection
            # for it. The stream-quiet check below is only for orphans whose
            # worker vanished (process restart/crash). Without this guard, a
            # healthy long run streaming huge tool-call args (which map to no
            # stream events) past 30min would be reaped as "quiet".
            task = tasks.process_state().active_runs.get(rid)
            if task is not None and not task.done():
                logger.info("chat_run_stale_skip_inprocess", run_id=rid)
                continue
            last_ms = await _stream_last_write_ms(rid)
            if last_ms is not None and (now_ms - last_ms) < settings._STALE_QUIET_SEC * 1000:
                # The stream is still producing: this is a long task, not a zombie — skip this round
                logger.info(
                    "chat_run_stale_skip_active",
                    run_id=rid,
                    quiet_sec=round((now_ms - last_ms) / 1000, 1),
                )
                continue
            reason = "run stalled: no stream activity (stale watchdog)"
        else:
            reason = "run exceeded hard max age (stale watchdog)"

        if not ownership._request_run_stop(
            rid,
            status="failed",
            error_message=reason,
            require_expired_lease=not hard_expired,
        ):
            continue  # race: the worker just beat us to writing the terminal state

        await writer._write_terminal_to_stream(
            rid,
            chat_id=cid or "",
            error_text="任务长时间无响应，已被系统中止，请重新发起",
        )

        payload = row.request_payload if isinstance(row.request_payload, dict) else {}
        task = tasks.process_state().active_runs.get(rid)
        if (
            task is not None
            and not task.done()
            and payload.get("kind") not in settings._COOPERATIVE_KINDS
        ):
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:
                logger.warning("stale_reaper_await_failed", run_id=rid, error=str(exc))
            if task.done():
                ownership._acknowledge_terminal_writer(rid)
        reaped += 1

    if reaped:
        logger.info("chat_run_stale_reaped", count=reaped)
    return reaped
