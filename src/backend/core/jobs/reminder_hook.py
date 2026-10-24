"""Job-ledger reminder contributed by the execution host."""
import asyncio
from typing import Optional
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage


class JobLedgerHook:
    def __init__(self, *, chat_id: Optional[str], user_id: Optional[str]) -> None:
        self._chat_id = chat_id or ""
        self._user_id = user_id or ""
        self._last_key: tuple | None = None

    def _ledger_lines(self) -> list[str]:
        from core.db.engine import SessionLocal
        from core.db.models import Job
        from core.jobs.store import JobService

        with SessionLocal() as db:
            jobs = (
                db.query(Job)
                .filter(Job.chat_id == self._chat_id, Job.user_id == self._user_id)
                .order_by(Job.created_at.desc())
                .limit(3)
                .all()
            )
            svc = JobService(db)
            lines = []
            for job in jobs:
                stats = svc.stats(job.job_id)
                unsettled = int(stats.get("pending", 0)) + int(stats.get("failed", 0))
                if unsettled <= 0 and job.status in ("completed", "cancelled"):
                    continue
                lines.append(
                    f"- 作业「{job.name or job.job_id}」（{job.job_id}，状态 {job.status}）："
                    f"总计 {stats.get('total', 0)}，已完成 {stats.get('done', 0)}，"
                    f"查无 {stats.get('not_found', 0)}，待办 {stats.get('pending', 0)}，"
                    f"失败 {stats.get('failed', 0)}"
                )
            return lines

    async def __call__(self, invocation):
        if invocation.operation_name != "agent_reasoning":
            return Decision()
        runtime = invocation.data["runtime"]
        if not self._chat_id:
            return
        key = (
            runtime.get("reply_id", "") or "",
            int(runtime.get("cur_iter", 0) or 0),
        )
        if key == self._last_key:
            return
        # HookBus runs sync handlers inline on the event loop; keep the ledger
        # query off it.
        lines = await asyncio.to_thread(self._ledger_lines)
        if not lines:
            return

        self._last_key = key
        reminder = (
            "本会话有尚未结算完的批量作业（数字取自台账，是可查证的事实，不是估计）：\n"
            + "\n".join(lines)
            + "\n\n还有待办或失败项时：用 run_job(action='resume', job_id=...) 续跑"
            "（已完成的项不会重做），或先查 run_job(action='status', job_id=...) 看明细。"
            "**不得**把未完成当作完成来交付；确实要收尾，也必须如实报出分母、完成数与未覆盖清单。"
        )
        return Decision.modify({"context_items": [*invocation.data.get("context_items", ()), {
            "content": f"<system-reminder>\n{reminder}\n</system-reminder>",
            "kind": "reminder", "origin": "harness:job_ledger", "trust": "system",
            "priority": 850, "token_budget": 4000,
        }]})



def job_ledger_hooks(request):
    if request.workflow_mode and not request.isolated and request.chat_id:
        yield HookSpec("job_ledger", HookStage.TRANSFORM_CONTEXT,
                       JobLedgerHook(chat_id=request.chat_id, user_id=request.current_user_id),
                       priority=500, failure_mode=FailureMode.OPEN,
                       mutable_fields=frozenset({"context_items"}))
