"""run_job 工具 —— 主对话智能体提交批量作业的唯一入口。

定位：这不是「又一个固定形状的批处理工具」，而是一个**可编程的编排面**。智能体先用
``write`` 把一段作业脚本写进沙箱，再用 ``run_job`` 提交；脚本里的控制流（循环、条件、
分层取数、动态收敛、嵌套 map）都是普通 Python，平台不需要知道任务在做什么。

与 bash / read / write / glob / grep 同级原生注册，不走 catalog 开关——「对话内默认
可用」就落在这里。
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, Dict, List, Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse

logger = logging.getLogger(__name__)


def _resp(payload: Dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=_json.dumps(payload, ensure_ascii=False))]
    )


def register_run_job(
    toolkit: Toolkit,
    *,
    user_id: str,
    chat_id: Optional[str],
    sandbox_session_id: Optional[str],
    allowed_tools: Optional[List[str]] = None,
    model_name: Optional[str] = None,
    model_provider_id: Optional[str] = None,
) -> None:
    """注册 run_job。

    ``allowed_tools`` 是本次会话解析出来的可用 MCP 工具面；作业脚本声明的工具会与它取
    交集，脚本不能提权到会话本身没有的能力。
    """

    async def run_job(
        action: str = "start",
        script_path: str = "",
        name: str = "",
        job_id: str = "",
        wait: bool = False,
        # 默认必须是**带三方库**的那个解释器。沙箱里裸 python3 是干净的系统解释器，
        # 连 openpyxl/pandas 都没有——默认值写成 python3 的直接后果是作业脚本第一行
        # import 就 ModuleNotFoundError 当场失败（实测踩过）。$PY_BIN 指向预装全套数据
        # 依赖的解释器，取不到时才退回 python3。
        interpreter: str = "${PY_BIN:-python3}",
        max_calls: int = 0,
        max_seconds: int = 0,
        concurrency: int = 8,
        dest_path: str = "",
        status: str = "",
        progress_wake_sec: int = 300,
        on_conflict: str = "block",
    ) -> ToolResponse:
        """提交并管理一次**批量作业**：对 N 个同构工作项做同一件事。

        什么时候必须用它：当工作项 ≥ 20 且彼此同构（补全表格某列、逐份审阅文档、
        逐个文件改代码、逐条数据打标），**禁止**在对话主循环里逐项处理——那样每一轮都要
        重发全部历史，成本随进度二次方增长，做不完。

        怎么用（三步）：

        1. 用 ``write`` 把作业脚本写到沙箱，例如 ``/workspace/jobs/fill.py``。
           脚本里 ``from hugagent_job import ledger, agent, job, log`` 即可，SDK 由系统注入：

             - ``ledger.seed([{"key": "r2", "payload": {...}}, ...])`` 建台账（按 key 幂等）
             - ``ledger.pending()`` 取待办；``ledger.update(key, status="done", result=...)`` 回写
             - ``ledger.stats()`` → ``{total, done, pending, settled, remaining, progressed}``
             - ``agent(prompt, schema=..., tools=["internet_search"], item_key=...)`` 派子智能体，
               返回按 schema 校验后的对象；**每项一次判断**用它，多轮工具循环也用它
             - ``job.map(items, fn, concurrency=8)`` 并发跑，单项异常自动隔离并写回 error
             - ``job.budget()`` → ``{calls_left, tokens_left, seconds_left}``；``log("...")`` 报进度

           其余一切用标准 Python：抓网页、解析、写 Excel、``subprocess`` 跑校验命令。
           **验收能机检就别烧模型**——``mypy`` / ``pytest`` / 一段校验函数都比 ``agent()`` 便宜。

        2. ``run_job(action="start", script_path="/workspace/jobs/fill.py", name="补全展品")``。
        3. 作业结束后用 ``action="export"`` 把台账导成沙箱里的 JSONL，再用 bash/python
           读它写产物（Excel、报告、校验）。**不要**把逐项结果读回对话。

        两条硬规矩：

        - **提交完就回话，不要轮询**。默认 ``wait=False``：作业在后台跑，工具立刻返回
          job_id。此时**先把当前这轮回复收掉**——告诉用户作业已在后台开始、进度看输入框
          上方的状态条、随时可以继续聊别的。作业跑完（以及每隔一段时间）系统会**自动
          唤醒本会话**让你播报，不需要你守着。反复调 ``action="status"`` 干等纯属浪费轮次。
          只有确信几十秒内能跑完的小作业才值得 ``wait=True`` 原地等——那会把整个会话
          阻塞住：用户看不到中间进度、插不上话、也没法中途改主意。
        - **逐项结果不进对话**。要用结果就 ``export`` 成文件再脚本处理。
        - **用户喊停就立刻停**。用户说「停止任务 / 别跑了 / 取消」时，先
          ``action="cancel"`` 停掉再回话，不要先解释也不要反问——台账已落库，
          之后 ``action="resume"`` 就能接着跑，停一下不损失任何已完成的工作。

        Args:
            action (`str`): ``start`` 提交 / ``status`` 查进度 / ``export`` 导出台账 /
                ``resume`` 断点续跑 / ``cancel`` 取消。
            script_path (`str`): 作业脚本在沙箱里的绝对路径（action=start 必填）。
            name (`str`): 作业名，便于在进度里辨认。
            job_id (`str`): status / resume / cancel 必填。
            wait (`bool`): **默认 false = 后台跑**：立即返回 job_id，作业跑完 / 每隔一段
                时间自动叫醒本会话播报，用户全程能看状态条、能插话、能取消。
                true 则原地阻塞到作业结束——只适合几十秒的小作业，长作业阻塞会让会话
                看起来「卡死在前台」，既看不到进度也没法中途干预。
            progress_wake_sec (`int`): 仅 wait=false：每隔多少秒把你叫回来播报一次进度
                （默认 300 秒＝5 分钟；0 表示只在终态叫一次）。被叫醒时只需转述进度，
                别重复提交作业。用户看到的实时进度条不靠它，它只决定你何时该介入。
            on_conflict (`str`): 仅 start，本会话已有在跑作业时怎么办。``block``（默认）
                拦下并把三条出路告诉你；``replace`` 先停掉旧作业再提交新的（换了脚本要重跑
                就用它）；``parallel`` 两份并行（确实互不相干时才用，预算是双份的）。
            interpreter (`str`): 跑脚本的解释器。默认 ``${PY_BIN:-python3}`` —— 沙箱里
                **裸 python3 是干净的系统解释器，连 openpyxl/pandas 都没有**，而 ``$PY_BIN``
                指向预装全套数据依赖的那个。除非有特别理由，**别覆盖这个默认值**；真要装
                额外的包再用 ``uv run --with <pkg> python``（沙箱可出网）。
            max_calls (`int`): 子作业调用次数上限，0 = 用默认。
            max_seconds (`int`): 墙钟秒数上限，0 = 用默认。
            concurrency (`int`): 子作业并发，默认 8，上限 16。
            dest_path (`str`): 仅 export：导出文件路径，默认
                ``/workspace/jobs/<job_id>_ledger.jsonl``。
            status (`str`): 仅 export：只导出这些状态，逗号分隔（如 ``"done,not_found"``）；
                留空导出全部。

        Returns:
            JSON。start/resume 返回 ``{ok, job_id, status, stats}``；status 返回
            ``{ok, status, stats, usage, budget_left}``。``stats.remaining>0`` 表示还没做完，
            必须如实告诉用户并给出续跑方式，不得当作完成。
        """
        from core.db.engine import SessionLocal
        from core.db.models import Job
        from core.services.job_service import JobService
        from orchestration import job_runtime

        act = (action or "start").strip().lower()

        if act == "start":
            if not script_path:
                return _resp({"ok": False, "error": "script_path 必填：先用 write 把作业脚本写进沙箱"})
            if not sandbox_session_id:
                return _resp({"ok": False, "error": "当前会话没有可用沙箱，无法提交作业"})

            # 同一会话不许叠加在跑的作业。被进度唤醒后"改个脚本再交一份"是很自然的动作，
            # 但旧作业不会自己消失：两份并存会同时烧预算、同时叫醒会话，用户在状态条上
            # 也分不清哪份才算数。要么先 cancel，要么 resume（已完成的项不会重做）。
            # 本会话已有在跑的作业时**默认**拦一道：默默叠加两份会双倍烧预算、双份叫醒会话，
            # 用户在状态条上也分不清哪份算数。但这只是默认，不是禁令——确实要换新版本时
            # 用 on_conflict 明说：'replace' 停掉旧的再跑新的，'parallel' 两份并行。
            # 判断权留给调用方，工具只负责让"叠加"变成一个显式选择而不是意外。
            conflict = (on_conflict or "block").strip().lower()
            if chat_id and conflict != "parallel":
                # 字段必须在 session 内取出：出了 with 块 ORM 实例就 detached，再读属性会炸
                with SessionLocal() as db:
                    live = (
                        db.query(Job)
                        .filter(
                            Job.chat_id == chat_id,
                            Job.status.in_(("pending", "running")),
                        )
                        .order_by(Job.created_at.desc())
                        .first()
                    )
                    live_id = live.job_id if live is not None else ""
                    live_name = (live.name or "未命名") if live is not None else ""
                if live_id and conflict == "replace":
                    await job_runtime.cancel_job(live_id, user_id=user_id)
                    logger.info("[run_job] replaced live job %s in chat %s", live_id, chat_id)
                elif live_id:
                    return _resp(
                        {
                            "ok": False,
                            "error": (
                                f"本会话已有在跑的作业 {live_id}（{live_name}）。三条路选一条："
                                "① 想用新脚本取代它 → 再调一次本工具并带 on_conflict='replace'"
                                "（自动停掉旧作业再提交新的）；"
                                f"② 旧作业只是中断、脚本没问题 → run_job(action='resume', job_id='{live_id}')"
                                "断点续跑，已完成的项不会重做；"
                                "③ 确实需要两份同时跑 → on_conflict='parallel'。"
                                "默认拦下只是为了让叠加成为显式选择，不是不让跑。"
                            ),
                            "job_id": live_id,
                            "hint": "on_conflict=replace|parallel",
                        }
                    )

            # 读脚本正文：既做存在性校验，也作为 job 的审计快照与 resume 依据。
            # ⚠️ 必须走 base64，不能直接 `cat`：沙箱 execute 回传的 stdout 会丢换行，
            # 脚本会被压成一行落地 → SyntaxError（实测踩过）。base64 -w0 保字节不变。
            try:
                code, out, err = await job_runtime._sbx_bash(
                    f"base64 -w0 {script_path} 2>/dev/null || base64 -i {script_path}",
                    session_id=sandbox_session_id,
                    user_id=user_id,
                    timeout=30,
                )
            except Exception as exc:  # noqa: BLE001
                return _resp({"ok": False, "error": f"读取脚本失败: {exc}"})
            if code != 0 or not (out or "").strip():
                return _resp({"ok": False, "error": f"脚本不存在或为空: {script_path} {(err or '')[:200]}"})
            import base64 as _b64dec

            try:
                out = _b64dec.b64decode("".join((out or "").split())).decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                return _resp({"ok": False, "error": f"脚本解码失败: {exc}"})
            if not out.strip():
                return _resp({"ok": False, "error": f"脚本为空: {script_path}"})

            budget: Dict[str, Any] = {"concurrency": concurrency}
            if max_calls:
                budget["max_calls"] = max_calls
            if max_seconds:
                budget["max_seconds"] = max_seconds

            try:
                jid = await job_runtime.start_job(
                    user_id=user_id,
                    chat_id=chat_id,
                    name=name or "批量作业",
                    script_path=script_path,
                    script_text=out,
                    session_id=sandbox_session_id,
                    budget=budget,
                    start_params={
                        "allowed_tools": list(allowed_tools or []),
                        "model_name": model_name,
                        "model_provider_id": model_provider_id,
                        # wait=False 时作业跑完没人叫醒本会话 → 打标，让驱动在终态入队一轮续跑
                        "wake_on_finish": not wait,
                        # 中途进度播报间隔（秒）；0 = 只在终态叫一次
                        "progress_wake_sec": max(0, int(progress_wake_sec)),
                    },
                    interpreter=interpreter or "${PY_BIN:-python3}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[run_job] start failed: %s", exc)
                return _resp({"ok": False, "error": f"作业启动失败: {exc}"})

            if not wait:
                job_runtime.spawn_background(jid, chat_id=chat_id)
                return _resp(
                    {
                        "ok": True,
                        "job_id": jid,
                        "status": "started",
                        "waited": False,
                        "next": (
                            "作业已在后台开始。**现在就结束这轮回复**：告诉用户作业已提交、"
                            "输入框上方的状态条会实时显示进度（几分之几、失败数、已运行多久），"
                            "期间可以继续聊别的、也可以点状态条上的按钮取消。"
                            "作业跑完和中途都会自动叫醒你播报，不要在这里轮询 action='status' 干等。"
                        ),
                    }
                )

            res = await job_runtime.run_and_wait(jid, chat_id=chat_id)
            with SessionLocal() as db:
                svc = JobService(db)
                stats = svc.stats(jid)
                job = svc.get(jid)
                usage = dict(job.usage or {}) if job else {}
            payload = {
                "ok": res.get("status") == "completed",
                "job_id": jid,
                "status": res.get("status"),
                "stats": stats,
                "usage": usage,
            }
            if res.get("error"):
                payload["error"] = res["error"]
            if stats.get("remaining", 0) > 0:
                payload["note"] = (
                    f"仍有 {stats['remaining']} 项未完成——必须如实告知用户并给出未覆盖清单与"
                    f"续跑方式（run_job action=resume job_id={jid}），不得当作已完成。"
                )
            # 脚本崩溃时把 runner 日志尾巴带回来，省一轮排查
            if res.get("status") == "failed" and sandbox_session_id:
                try:
                    payload["runner_log"] = (
                        await job_runtime.read_runner_log(
                            jid, user_id=user_id, session_id=sandbox_session_id
                        )
                    )[-1500:]
                except Exception:  # noqa: BLE001
                    pass
            return _resp(payload)

        if not job_id:
            return _resp({"ok": False, "error": f"action={act} 需要 job_id"})

        # job_id 容错 + 找不到时把本会话的作业列出来。
        # 实测踩过：模型凭记忆抄 id 时漏了 "job_" 前缀，只回一句「job 不存在」它无从自救，
        # 结果决定把 568 次子调用整个重跑一遍。错误信息必须自带下一步。
        with SessionLocal() as db:
            svc = JobService(db)
            resolved = svc.get(job_id)
            if (resolved is None or resolved.user_id != user_id) and not job_id.startswith("job_"):
                cand = svc.get(f"job_{job_id}")
                if cand is not None and cand.user_id == user_id:
                    resolved, job_id = cand, f"job_{job_id}"
            if resolved is None or resolved.user_id != user_id:
                from core.db.models import Job

                rows = (
                    db.query(Job)
                    .filter(Job.user_id == user_id, Job.chat_id == chat_id)
                    .order_by(Job.created_at.desc())
                    .limit(5)
                    .all()
                )
                return _resp(
                    {
                        "ok": False,
                        "error": f"job 不存在或无权访问: {job_id}",
                        "jobs_in_this_chat": [
                            {
                                "job_id": r.job_id,
                                "name": r.name,
                                "status": r.status,
                                "stats": svc.stats(r.job_id),
                            }
                            for r in rows
                        ],
                        "hint": "用上面列表里的完整 job_id 重试（注意 job_ 前缀）；"
                        "已经跑完的作业**不要重跑**，直接 export 取结果。",
                    }
                )

        if act == "status":
            with SessionLocal() as db:
                svc = JobService(db)
                job = svc.get(job_id)
                if job is None or job.user_id != user_id:
                    return _resp({"ok": False, "error": "job 不存在或无权访问"})
                return _resp(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "status": job.status,
                        "stats": svc.stats(job_id),
                        "usage": dict(job.usage or {}),
                        "budget_left": svc.budget_left(job_id),
                        "error": job.error_message,
                    }
                )

        if act == "export":
            # 台账在 DB，逐项结果**不进对话上下文**——所以导出的方式是写成沙箱里的
            # JSONL 文件，之后用 bash/python 处理（写 Excel、跑校验、喂评审）。
            if not sandbox_session_id:
                return _resp({"ok": False, "error": "当前会话没有可用沙箱"})
            with SessionLocal() as db:
                svc = JobService(db)
                job = svc.get(job_id)
                if job is None or job.user_id != user_id:
                    return _resp({"ok": False, "error": "job 不存在或无权访问"})
                wanted = [s.strip() for s in (status or "").split(",") if s.strip()] or [
                    "done",
                    "not_found",
                    "failed",
                    "needs_review",
                    "pending",
                ]
                rows = []
                for st in wanted:
                    rows.extend(svc.pending(job_id, status=st))
                stats = svc.stats(job_id)

            dest = dest_path or f"/workspace/jobs/{job_id}_ledger.jsonl"
            body = "\n".join(_json.dumps(r, ensure_ascii=False) for r in rows)
            # 分块写 + 读回校验：单条 bash 携带大 base64 到约 170KB 会**静默失败**
            # （exit=0、stderr 空、文件不存在）。568 行台账正好落在这个区间——历史上
            # 因此出现过「导出报成功 rows=568，文件却从来没出现」。成功与否只认沙箱
            # 里读回的真实字节数，绝不用 Python 侧的行数报平安。
            try:
                ok_write, detail = await job_runtime.write_sandbox_file(
                    dest, body, session_id=sandbox_session_id, user_id=user_id
                )
            except Exception as exc:  # noqa: BLE001
                return _resp({"ok": False, "error": f"导出失败: {exc}"})
            if not ok_write:
                return _resp({"ok": False, "error": f"导出失败（落盘未通过校验）: {detail}"})
            return _resp(
                {
                    "ok": True,
                    "job_id": job_id,
                    "path": dest,
                    "rows": len(rows),
                    "bytes": detail,
                    "stats": stats,
                    "hint": "每行一个 JSON：{key, status, payload, result, review, attempts}。"
                    "用 bash/python 读这个文件写产物，不要把逐项内容读回对话。",
                }
            )

        if act == "cancel":
            ok = await job_runtime.cancel_job(job_id, user_id=user_id)
            return _resp({"ok": ok, "job_id": job_id, "status": "cancelled" if ok else "unknown"})

        if act == "resume":
            res = await job_runtime.resume_job(job_id, user_id=user_id, chat_id=chat_id)
            if not res.get("ok"):
                return _resp({"ok": False, "error": res.get("error")})
            if not wait:
                job_runtime.spawn_background(job_id, chat_id=chat_id)
                return _resp({"ok": True, "job_id": job_id, "status": "running", "waited": False})
            done = await job_runtime.run_and_wait(job_id, chat_id=chat_id)
            with SessionLocal() as db:
                stats = JobService(db).stats(job_id)
            return _resp(
                {"ok": done.get("status") == "completed", "job_id": job_id,
                 "status": done.get("status"), "stats": stats}
            )

        return _resp({"ok": False, "error": f"未知 action: {act}"})

    toolkit.register_tool_function(run_job)
