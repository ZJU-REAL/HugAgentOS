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

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.db.engine import SessionLocal
from core.infra.responses import success_response
from core.services.job_service import JobService

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


class AgentBody(BaseModel):
    # 字段名叫 schema_ 是为了避开 BaseModel 的保留名，对外仍是 "schema"
    model_config = ConfigDict(populate_by_name=True)

    prompt: str = ""
    schema_: Optional[Dict[str, Any]] = Field(default=None, alias="schema")
    tools: List[str] = []
    model: Optional[str] = None
    max_attempts: int = 2
    # 必须收 int：业务主键十有八九是行号/序号，脚本自然会写 item_key=it["seq"]。
    # Pydantic v2 不做 int→str 强转，只声明 str 会让整轮作业被 422 挡在门外——
    # 事故里 568 项全军覆没、模型一次都没被调用，就是这个类型洁癖的代价。
    item_key: Optional[Union[str, int]] = None

    @field_validator("item_key")
    @classmethod
    def _key_to_str(cls, v: Optional[Union[str, int]]) -> Optional[str]:
        return None if v is None or v == "" else str(v)


class LedgerBody(BaseModel):
    op: str
    items: Optional[List[Dict[str, Any]]] = None
    # 同 AgentBody.item_key：主键常常是整数序号，别让类型把回写挡在门外
    key: Optional[Union[str, int]] = None
    status: Optional[str] = None
    limit: Optional[int] = None
    result: Optional[Any] = None
    review: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    bump_attempts: bool = False


class LogBody(BaseModel):
    message: Optional[str] = None
    lifecycle: Optional[str] = None
    error: Optional[str] = None


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


# ── 轻量子作业执行 ──────────────────────────────────────────────────────


# 工具级基础设施故障的特征串。命中时这一项不能被当成业务结论（"查无"），
# 必须作为可重试的失败抛回脚本——否则环境故障会被固化成数据。
# （实测踩过：搜索配额打爆后 337/354 项被写成"未查询到公开展品信息"并标 done。）
_INFRA_FAILURE_MARKERS = (
    "用量限制",
    "quota",
    "rate limit",
    "too many requests",
    "调用失败",
    "connection error",
    "timeout",
    "timed out",
    "503",
    "502",
    "429",
)


def _looks_like_infra_failure(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _INFRA_FAILURE_MARKERS)


async def _run_light_agent(
    prompt: str,
    *,
    tools: List[str],
    user_id: str,
    chat_id: str,
    run_id: str,
    journal_owner: str,
    model_name: Optional[str],
    model_provider_id: Optional[str],
) -> tuple[str, bool]:
    """无历史、无沙箱、无技能、无子智能体的一次性子作业。

    这正是「去掉共享上下文 / 流式回灌 / 历史累积」后的执行路径：每次调用的上下文只有
    这一条 prompt，成本与工作项总数无关。
    """
    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients
    from orchestration.streaming import StreamingAgent

    agent, clients = await create_agent_executor(
        enabled_skill_ids=[],
        enabled_mcp_ids=list(tools or []),
        enabled_kb_ids=[],
        disable_tools=not tools,
        chat_mode="fast",
        isolated=True,
        allow_bash=False,
        read_only=True,
        max_iters=6,
        model_name=model_name,
        model_provider_id=model_provider_id,
        current_user_id=user_id,
        chat_id=chat_id,
        run_id=run_id,
        journal_owner=journal_owner,
        top_level_chat=False,
    )
    text = ""
    tool_calls = 0
    tool_failures = 0
    infra_failure = False
    try:
        sa = StreamingAgent(agent, clients)
        async for et, payload in sa.stream(
            [{"role": "user", "content": prompt}],
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "run_id": run_id,
                "journal_owner": journal_owner,
                "enable_thinking": False,
                "chat_mode": "fast",
            },
        ):
            if et == "text_delta":
                text += payload
            elif et == "tool_result":
                tool_calls += 1
                content = str((payload or {}).get("content") or "")
                failed = str((payload or {}).get("status") or "") in (
                    "error",
                    "denied",
                    "interrupted",
                )
                if failed or _looks_like_infra_failure(content):
                    tool_failures += 1
                    if _looks_like_infra_failure(content):
                        infra_failure = True
            elif et == "error":
                logger.warning("[job-agent] sub-agent error: %s", payload)
                infra_failure = True
                break
    finally:
        await close_clients(clients)

    # 声明了工具却每一次都失败 → 这一轮没有任何证据，结论不可信
    if tools and tool_calls > 0 and tool_failures == tool_calls:
        infra_failure = True
    # 模型自己在正文里如实说了"工具调用失败/超限"，同样不能当业务结论
    if _looks_like_infra_failure(text):
        infra_failure = True
    return text, infra_failure


# ── 端点 ──────────────────────────────────────────────────────────────


@router.post("/{job_id}/agent")
async def job_agent(
    job_id: str,
    body: AgentBody,
    x_job_token: Optional[str] = Header(None, alias="X-Job-Token"),
):
    ctx = _auth(job_id, x_job_token)

    with SessionLocal() as db:
        left = JobService(db).budget_left(job_id)
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

    attempts = max(1, min(int(body.max_attempts or 2), 4))
    started = time.monotonic()
    last_text = ""
    result: Any = None

    # 调用即落账（服务端安全网）：脚本自己的 ledger.update 才是权威，但脚本可能压根没写回
    # ——历史事故正是如此：568 项全程 pending、调用真在跑、成果全丢，外面看不出任何异常。
    # 所以只要带了 item_key，服务端先把该项标成 running 并累加 attempts；脚本随后的回写会
    # 覆盖它（顺序天然如此），进度条与中途唤醒因此永远有真实分母。
    if body.item_key:
        try:
            with SessionLocal() as db:
                JobService(db).update_item(
                    job_id, body.item_key, status="running", bump_attempts=True
                )
        except Exception:  # noqa: BLE001 —— 落账失败绝不能挡住真正的作业
            logger.warning("[job-agent] job=%s pre-mark failed key=%s", job_id, body.item_key)

    gate = _gate(job_id, left.get("concurrency", 8))
    infra_failed = False
    async with _GLOBAL_GATE, gate:
        for i in range(attempts):
            try:
                from core.services.run_journal import durable_run_binding

                async with durable_run_binding(
                    user_id=ctx["user_id"],
                    chat_id=ctx["chat_id"],
                    kind="internal_job_agent",
                    external_id=f"{job_id}:{body.item_key or 'adhoc'}:{i}",
                    request_payload={"job_id": job_id, "item_key": body.item_key},
                    recovery_snapshot={
                        "worker_args": {
                            "context": {
                                "mcp_ids": list(tools),
                                "skill_ids": [],
                                "model_name": body.model or ctx["model_name"],
                                "model_provider_id": ctx["model_provider_id"],
                                "chat_mode": "fast",
                            }
                        }
                    },
                ) as binding:
                    last_text, infra_failed = await _run_light_agent(
                        (
                            prompt
                            if i == 0
                            else prompt + "\n\n上一次输出无法解析为合法 JSON，请只输出 JSON。"
                        ),
                        tools=tools,
                        user_id=ctx["user_id"],
                        chat_id=binding.chat_id,
                        run_id=binding.run_id,
                        journal_owner=binding.owner,
                        model_name=body.model or ctx["model_name"],
                        model_provider_id=ctx["model_provider_id"],
                    )
            except Exception as exc:  # noqa: BLE001
                from core.services.tool_effect_ledger import find_tool_outcome_unknown

                if find_tool_outcome_unknown(exc) is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="工具调用结果尚待安全恢复，本项已暂停，不能自动重试",
                    ) from exc
                logger.warning("[job-agent] job=%s attempt=%d failed: %s", job_id, i, exc)
                last_text, infra_failed = "", True
                continue
            # 工具全挂/配额打爆：这一轮没有任何证据，**不能**把它当成业务结论回给脚本，
            # 否则"搜不到"会被固化进数据。抛可重试错误，让该项留在台账里待续跑。
            if infra_failed:
                logger.warning("[job-agent] job=%s attempt=%d infra failure", job_id, i)
                continue
            if not schema:
                result = last_text
                break
            parsed = _parse_schema(last_text, schema)
            if parsed is not None:
                result = parsed
                break

    duration_ms = int((time.monotonic() - started) * 1000)
    ok = result is not None
    # 归因必须分流：工具挂了和模型输出不合 schema 是两回事，共用一条文案会把排障线索抹平
    # （历史事故：17 次失败全记「结构化输出解析失败」，实际分不清是搜索不通还是模型不听话）。
    reason = (
        None
        if ok
        else ("工具不可用/配额/超时，本项未取得证据" if infra_failed else "结构化输出解析失败")
    )
    with SessionLocal() as db:
        svc = JobService(db)
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
            # 503 = 可重试：SDK 会退避重试，仍失败则该项记 failed 留在台账等续跑，
            # 而不是被写成"查无"。环境故障绝不能变成业务结论。
            raise HTTPException(
                status_code=503,
                detail=f"工具不可用（配额/连接/超时），本项未取得证据：{(last_text or '')[:200]}",
            )
        raise HTTPException(
            status_code=422,
            detail=f"子作业未产出合法结果（{attempts} 次尝试）：{(last_text or '')[:200]}",
        )
    return success_response(data=result)


@router.post("/{job_id}/ledger")
async def job_ledger(
    job_id: str,
    body: LedgerBody,
    x_job_token: Optional[str] = Header(None, alias="X-Job-Token"),
):
    _auth(job_id, x_job_token)
    op = (body.op or "").strip()

    with SessionLocal() as db:
        svc = JobService(db)
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
async def job_log(
    job_id: str,
    body: LogBody,
    x_job_token: Optional[str] = Header(None, alias="X-Job-Token"),
):
    ctx = _auth(job_id, x_job_token)

    if body.lifecycle:
        with SessionLocal() as db:
            svc = JobService(db)
            if body.lifecycle == "running":
                svc.mark_running(job_id)
            elif body.lifecycle in ("completed", "failed"):
                svc.finish(job_id, body.lifecycle, error=body.error)
        return success_response(data={"ok": True})

    msg = (body.message or "").strip()
    if msg:
        logger.info("[job %s] %s", job_id, msg[:500])
        try:
            from orchestration.job_runtime import _emit_progress

            _emit_progress(ctx["chat_id"], msg[:200])
        except Exception:  # noqa: BLE001
            pass
    return success_response(data={"ok": True})
