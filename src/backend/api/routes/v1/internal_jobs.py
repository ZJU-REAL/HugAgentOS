"""作业脚本的回调端点 —— 沙箱里的作业脚本经此请求后端。

这是「模型凭据不进沙箱」的关键：脚本自己没有任何 key，需要一次模型判断时就带 job token
POST 到这里，由后端代持凭据派出子智能体。token 能做的事只有三件——派子作业、读写本 job
的台账、报进度；换不到模型端点、碰不到别的 job、碰不到任何用户数据接口。

端点：
    POST /v1/internal/jobs/{job_id}/agent    派一个无历史子智能体，同步返回结构化结果
    POST /v1/internal/jobs/{job_id}/ledger   seed / pending / update / stats / budget
    POST /v1/internal/jobs/{job_id}/log      进度上报与生命周期（running/completed/failed）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Union

from core.db.engine import SessionLocal
from core.infra.responses import success_response
from core.jobs.store import JobService
from fastapi import APIRouter, Header, HTTPException

from .job_callback_session import callback_session
from core.db.threads import run_in_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/internal/jobs", tags=["internal-jobs"])

# 每个 job 的在途子作业闸门（进程内）：脚本可能一次打出 concurrency 个并发回调，
# 这里按 job 预算收口，全局闸再防单会话吃光后端。
_job_gates: Dict[str, asyncio.Semaphore] = {}
_GLOBAL_GATE = asyncio.Semaphore(24)


def _gate(job_id: str, concurrency: int) -> asyncio.Semaphore:
    gate = _job_gates.get(job_id)
    if gate is None:
        gate = asyncio.Semaphore(max(1, min(int(concurrency or 8), 16)))
        _job_gates[job_id] = gate
    return gate


# ── 请求体 ────────────────────────────────────────────────────────────


from .job_schemas import AgentBody, LedgerBody, LogBody

# ── 鉴权 ──────────────────────────────────────────────────────────────


def _auth(job_id: str, token: Optional[str]):
    """token 校验 → 返回 (user_id, chat_id, budget, allowed_tools, model_params)。

    终态 job 一律拒绝：沙箱会被后续会话复用，旧脚本不得借尸还魂。
    """
    if not token:
        raise HTTPException(status_code=401, detail="缺少 job token")
    with SessionLocal() as db:
        job = JobService(db).verify_token(job_id, token)
        if job is None:
            raise HTTPException(status_code=403, detail="job token 无效或作业已结束")
        start_params = dict((job.extra_data or {}).get("start_params") or {})
        return {
            "user_id": job.user_id,
            "chat_id": job.chat_id,
            "budget": dict(job.budget or {}),
            "allowed_tools": list(start_params.get("allowed_tools") or []),
            "model_name": start_params.get("model_name"),
            "model_provider_id": start_params.get("model_provider_id"),
            "project_ctx": start_params.get("project_ctx"),
            "parent_runtime": start_params.get("parent_runtime"),
        }


# ── 结构化输出 ─────────────────────────────────────────────────────────


def _json_candidates(text: str):
    if not text:
        return
    yield text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        yield m.group(1).strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        yield text[s : e + 1]
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e != -1 and e > s:
        yield text[s : e + 1]


def _parse_schema(text: str, schema: Dict[str, Any]) -> Optional[Any]:
    """宽进严出：先尽力从自由文本里抠出 JSON，再按 schema 校验。"""
    for cand in _json_candidates(text):
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        try:
            import jsonschema

            jsonschema.validate(obj, schema)
        except ImportError:
            required = list((schema or {}).get("required") or [])
            if isinstance(obj, dict) and all(k in obj for k in required):
                return obj
            continue
        except Exception:
            continue
        return obj
    return None


def _schema_hint(schema: Dict[str, Any]) -> str:
    return (
        "\n\n【输出格式（强制）】只输出一个 JSON 对象，不要任何解释文字、不要代码块以外的内容。"
        "必须严格满足以下 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False)[:2000]
    )


from core.jobs.agent import run_derived_agent, run_with_job_fence

# ── 端点 ──────────────────────────────────────────────────────────────


@router.post("/{job_id}/agent")
async def job_agent(
    job_id: str,
    body: AgentBody,
    x_job_token: Optional[str] = Header(None, alias="X-Job-Token"),
):
    ctx = _auth(job_id, x_job_token)
    if not isinstance(ctx.get("parent_runtime"), dict):
        raise HTTPException(409, "作业缺少主智能体运行配置，请重新提交工作流")

    def validate_execution():
        _auth(job_id, x_job_token)
        with SessionLocal() as db:
            remaining = JobService(db).budget_left(job_id)
        if remaining["seconds_left"] <= 0 or remaining["calls_left"] <= 0:
            raise HTTPException(429, "作业执行预算已用尽")

    def _budget_left(db):
        left = JobService(db).budget_left(job_id)
        return left
    left = await run_in_session(_budget_left)
    if left["calls_left"] <= 0:
        raise HTTPException(status_code=429, detail="作业子调用预算已用尽")
    if left["seconds_left"] <= 0:
        raise HTTPException(status_code=429, detail="作业墙钟预算已用尽")

    # 工具面 = 脚本声明 ∩ 发起会话的工具面。脚本不能提权到会话本身没有的能力。
    declared = [t for t in (body.tools or []) if isinstance(t, str)]
    allowed = ctx["allowed_tools"]
    tools = [t for t in declared if t in allowed] if allowed else []
    dropped = sorted(set(declared) - set(tools))
    if dropped:
        logger.info("[job-agent] job=%s dropped out-of-scope tools=%s", job_id, dropped)

    prompt = body.prompt or ""
    schema = body.schema_ if isinstance(body.schema_, dict) else None
    if schema:
        prompt = prompt + _schema_hint(schema)

    started = time.monotonic()
    last_text = ""
    result: Any = None

    # 调用即落账（服务端安全网）：脚本自己的 ledger.update 才是权威，但脚本可能压根没写回
    # ——历史事故正是如此：568 项全程 pending、调用真在跑、成果全丢，外面看不出任何异常。
    # 所以只要带了 item_key，服务端先把该项标成 running 并累加 attempts；脚本随后的回写会
    # 覆盖它（顺序天然如此），进度条与中途唤醒因此永远有真实分母。
    if body.item_key:
        try:
            with callback_session(job_id, x_job_token) as svc:
                svc.update_item(job_id, body.item_key, status="running", bump_attempts=True)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 —— 落账失败绝不能挡住真正的作业
            logger.warning("[job-agent] job=%s pre-mark failed key=%s", job_id, body.item_key)

    gate = _gate(job_id, left.get("concurrency", 8))
    infra_failed = False
    async with _GLOBAL_GATE, gate:
        _auth(job_id, x_job_token)
        try:
            from core.runs.journal import durable_run_binding

            async with durable_run_binding(
                user_id=ctx["user_id"],
                chat_id=None,
                kind="internal_job_agent",
                external_id=f"{job_id}:{body.item_key or 'adhoc'}",
                request_payload={
                    "job_id": job_id,
                    "item_key": body.item_key,
                    "parent_chat_id": ctx["chat_id"],
                },
                recovery_snapshot={
                    "worker_args": {
                        "context": {
                            **(ctx.get("project_ctx") or {}),
                            "project_ctx": ctx.get("project_ctx"),
                            "mcp_ids": list(ctx["parent_runtime"].get("enabled_mcp_ids") or []),
                            "skill_ids": list(ctx["parent_runtime"].get("enabled_skill_ids") or []),
                            "tool_approval_mode": "full",
                            "model_name": body.model or ctx["model_name"],
                            "model_provider_id": ctx["model_provider_id"],
                            "chat_mode": ctx["parent_runtime"].get("chat_mode"),
                        }
                    }
                },
            ) as binding:
                last_text, infra_failed = await run_with_job_fence(
                    run_derived_agent(
                        prompt,
                        parent_runtime=ctx["parent_runtime"],
                        tools=tools if declared else None,
                        user_id=ctx["user_id"],
                        chat_id=binding.chat_id,
                        run_id=binding.run_id,
                        journal_owner=binding.owner,
                        model_name=body.model or ctx["model_name"],
                        model_provider_id=ctx["model_provider_id"],
                        project_ctx=ctx.get("project_ctx"),
                    ),
                    validate_execution,
                )
        except HTTPException as exc:
            if exc.status_code == 429:
                raise HTTPException(409, "作业预算已耗尽，本项已停止，不自动重试") from exc
            raise
        except Exception as exc:
            from core.runs.tool_effects.errors import find_tool_outcome_unknown

            if find_tool_outcome_unknown(exc) is not None:
                raise HTTPException(
                    409, "工具调用结果尚待安全恢复，本项已暂停，不能自动重试"
                ) from exc
            logger.warning("[job-agent] job=%s failed: %s", job_id, exc)
            last_text, infra_failed = "", True
        if not infra_failed:
            result = _parse_schema(last_text, schema) if schema else last_text

    duration_ms = int((time.monotonic() - started) * 1000)
    ok = result is not None
    # 归因必须分流：工具挂了和模型输出不合 schema 是两回事，共用一条文案会把排障线索抹平
    # （历史事故：17 次失败全记「结构化输出解析失败」，实际分不清是搜索不通还是模型不听话）。
    reason = (
        None
        if ok
        else ("工具不可用/配额/超时，本项未取得证据" if infra_failed else "结构化输出解析失败")
    )
    with callback_session(job_id, x_job_token) as svc:
        svc.add_usage(job_id, calls=1)
        svc.record_call(
            job_id,
            item_key=body.item_key,
            prompt=prompt,
            model=body.model or ctx["model_name"],
            duration_ms=duration_ms,
            status="success" if ok else "failed",
            error=reason,
        )
        # 结账：脚本的回写在这之后发生、会覆盖这里的判断——这只是脚本没回写时的兜底真相
        if body.item_key:
            try:
                if ok:
                    svc.update_item(
                        job_id,
                        body.item_key,
                        status="done",
                        result=result if isinstance(result, dict) else {"text": result},
                    )
                else:
                    svc.update_item(job_id, body.item_key, status="failed", error=reason)
            except Exception:  # noqa: BLE001
                logger.warning("[job-agent] job=%s post-mark failed key=%s", job_id, body.item_key)
    if not ok:
        if infra_failed:
            # A full agent may have already changed external state before failing.
            # Return a non-retryable error rather than silently repeating its actions.
            raise HTTPException(
                status_code=409,
                detail=f"本项执行失败，请检查已有操作后再决定是否续跑：{(last_text or '')[:200]}",
            )
        raise HTTPException(
            status_code=422,
            detail=f"派生智能体输出格式无效，完整任务不自动重跑：{(last_text or '')[:200]}",
        )
    return success_response(data=result)


@router.post("/{job_id}/ledger")
def job_ledger(
    job_id: str,
    body: LedgerBody,
    x_job_token: Optional[str] = Header(None, alias="X-Job-Token"),
):
    _auth(job_id, x_job_token)
    op = (body.op or "").strip()

    with callback_session(job_id, x_job_token) as svc:
        db = svc.db
        if op == "seed":
            return success_response(data=svc.seed(job_id, body.items or []))
        if op == "pending":
            return success_response(
                data=svc.pending(job_id, status=body.status or "pending", limit=body.limit)
            )
        if op == "update":
            if body.key in (None, ""):
                raise HTTPException(status_code=400, detail="update 需要 key")
            ok = svc.update_item(
                job_id,
                str(body.key),
                status=body.status,
                result=body.result,
                review=body.review,
                error=body.error,
                bump_attempts=body.bump_attempts,
            )
            # ok=False 表示台账里根本没有这个 key（多半是漏了 seed）。这个信号必须回给脚本：
            # 事故中脚本直连 job.map 没建台账，568 次回写全部打空，外面看到的只有"进度 0"。
            return success_response(data={"ok": ok, "known_key": ok})
        if op == "stats":
            stats = svc.stats(job_id)
            # progressed：本次统计相对上次是否有推进，脚本据此判停滞（不必自己记账）
            prev = int((svc.get(job_id).extra_data or {}).get("last_settled", -1))
            stats["progressed"] = stats["settled"] != prev
            job = svc.get(job_id)
            meta = dict(job.extra_data or {})
            meta["last_settled"] = stats["settled"]
            job.extra_data = meta
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(job, "extra_data")
            db.commit()
            return success_response(data=stats)
        if op == "budget":
            return success_response(data=svc.budget_left(job_id))

    raise HTTPException(status_code=400, detail=f"未知 ledger op: {op}")


@router.post("/{job_id}/log")
def job_log(
    job_id: str,
    body: LogBody,
    x_job_token: Optional[str] = Header(None, alias="X-Job-Token"),
):
    ctx = _auth(job_id, x_job_token)

    if body.lifecycle:
        with callback_session(job_id, x_job_token) as svc:
            if body.lifecycle == "running":
                svc.mark_running(job_id)
            elif body.lifecycle in ("completed", "failed"):
                svc.finish(job_id, body.lifecycle, error=body.error)
        return success_response(data={"ok": True})

    msg = (body.message or "").strip()
    if msg:
        logger.info("[job %s] %s", job_id, msg[:500])
        try:
            from core.jobs.notifications import emit_progress as _emit_progress

            _emit_progress(ctx["chat_id"], msg[:200])
        except Exception:  # noqa: BLE001
            pass
    return success_response(data={"ok": True})
