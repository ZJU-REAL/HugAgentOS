"""Autonomous Loop driver — run-level self-driving loop (the main axis for long-running autonomous work).

This overhaul makes the three pillars of Claude Code's "external harness" the skeleton of
the loop **itself**, replacing the old "worker self-managed todos + single-goal stagnation
convergence" design:

  1. **Driver-owned requirement ledger feature_list.json** (formerly feature_list.json) — the
     objective is decomposed once by the initializer into a set of discrete, independently
     verifiable requirements; the worker has **no authority to delete or modify** the ledger and
     may only implement the current item; `passes` flips to true only after the driver has judged
     it. All passes → loop achieved (prevents cheating through).
  2. **Hard injection of one requirement at a time** — each iteration the driver feeds only the
     highest-priority unfinished requirement to the worker (counters greed-induced context blowup).
  3. **Read-only reviewer sub-agent verdict** — before flipping any item, the driver spawns an
     **independent, read-only** reviewer sub-agent (orchestration/subagents/loop_reviewer), bound
     to the same project sandbox as the worker, which **personally opens the real produced files**
     to verify the requirement landed, and **never trusts the worker's self-reported text**. A
     "done" verdict must also pass an independent second-pass re-check.
     (The old "script verification / verify.sh freeze / numeric-score stagnation convergence" has
     been removed entirely — see the lessons from trace 435be138.)

Supporting pieces: git checkpoints (commit a known-good point on every flipped item, handoff uses
git diff), budget circuit-breaker, resume-from-checkpoint, HITL, per-requirement attempt cap
(prevents infinite loops). The loop is **fully bound to the project** the user selected in the
input box: the worker operates directly in the project folder (where the site source lives),
changes land in the project, publishing goes through publish_site — no longer an isolated
/workspace draft.

Design: internal design docs. State persists to disk, not to
context (Ralph/Codex): feature_list.json / handoffs.md / PROGRESS.md live in the persistent
sandbox; each iteration the worker restarts with a fresh context + the previous handoff.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.infra.logging import get_logger
from orchestration.loop_evaluator import (
    CONTINUE,
    DONE,
    NEED_HUMAN,
    GoalSpec,
    decompose_requirements,
    extract_acceptance_criteria,
)
from orchestration.loop_planner import (
    plan_requirements,
    replan_remaining,
    scout_workspace,
)
from orchestration.subagents.loop_reviewer import review_requirement

logger = get_logger(__name__)

EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]
CancelFn = Callable[[], bool]
SteeringFn = Callable[[], List[str]]


def _env_int(name: str, default: int, *, floor: int = 1) -> int:
    import os

    try:
        return max(floor, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# 去预算化后的防死循环硬后备（不是预算——预算默认不设；这是「循环失控」的最后保险丝，
# 远大于任何正常任务的轮数）。连续基础设施故障轮的熔断阈值同理。
def _hard_max_iters() -> int:
    return _env_int("LOOP_HARD_MAX_ITERS", 500)


def _max_consecutive_infra() -> int:
    return _env_int("LOOP_MAX_CONSECUTIVE_INFRA", 6)


def _max_replans() -> int:
    return _env_int("LOOP_MAX_REPLANS", 2, floor=0)


def _wrapup_enabled() -> bool:
    import os

    return os.getenv("LOOP_WRAPUP", "true").strip().lower() in ("1", "true", "yes")


def _build_wrapup_prompt(objective: str, ledger: Dict[str, Any], in_project: bool) -> str:
    done = [r for r in ledger["requirements"] if r.get("passes")]
    undone = [r for r in ledger["requirements"] if not r.get("passes")]
    where = "项目文件夹" if in_project else "/workspace"
    return (
        "# 收尾交付（最后一轮，不再评审）\n"
        f"自主任务到此收尾。总目标：\n{objective}\n\n"
        "## 已完成并通过评审的需求\n"
        + ("\n".join(f"- {r['id']}: {r['description']}" for r in done) or "（无）")
        + "\n\n## 未完成的需求\n"
        + ("\n".join(f"- {r['id']}: {r['description']}" for r in undone) or "（无）")
        + f"\n\n## 你的任务\n1. 检查 {where} 里的现有成果，把**已完成部分**整合成对用户"
        "可直接使用的交付形态（该合并的合并、该注册 artifact 的注册"
        + ("、站点有改动就 publish_site 发新版" if in_project else "")
        + "）。\n2. 用中文给用户写一段简明收尾说明：交付了什么、在哪里取用、"
        "未完成的部分还差什么、建议下一步。**如实说明，不得声称未完成的已完成。**\n"
        "3. 只整合与说明，不要开工做新需求。"
    )

# Loop thresholds come from the active orchestration profile, resolved **per
# run** and **per tenant** (core.evolution.policies projects the profile onto
# the knobs this loop uses).
#
# Resolving at import — which is what this did — had two failure modes that
# never surfaced as errors: a newly published profile did not take effect until
# the process restarted, and different replicas ran different policies until all
# of them had. Resolving without a tenant had a third: one tenant's published
# retry counts and budget multiplier silently became everyone's.
def _resolve_loop_policy(*, tenant_id: str, user_id: str = ""):
    from core.evolution.policies import load_loop_policy

    return load_loop_policy(tenant_id=tenant_id, user_id=user_id)

_WORKSPACE = "/workspace"
_LEDGER_PATH = f"{_WORKSPACE}/feature_list.json"


def _loop_tool_result_limit() -> int:
    """Per-tool-result context cap (tokens) for loop worker/reviewer agents.

    Document-heavy loop workloads measured 650k–1M tokens per iteration under
    the general 20k cap (every round re-sends all accumulated tool results).
    6k keeps greps/summaries useful while cutting the quadratic growth ~3×;
    full content remains readable on demand from /workspace/.offload.
    """
    import os

    try:
        return max(1_000, int(os.getenv("LOOP_TOOL_RESULT_LIMIT", "6000")))
    except ValueError:
        return 6_000


@dataclass
class LoopBudget:
    """预算已降级为**可选约束**：字段 <=0 一律视为「不限」，且默认全部不限——
    循环的停止条件回归「账本全部通过 / 停滞无解 / 用户取消」（能完成任务比省预算重要）。
    显式传正数仍然生效（想限就限）；防失控由 LOOP_HARD_MAX_ITERS 硬后备兜底。"""

    max_iters: int = 0
    max_wall_clock_s: float = 0.0
    max_tokens: int = 0
    max_subagents: int = 0  # reserved

    def snapshot(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LoopResult:
    status: str
    iterations: int
    final_score: Optional[float]
    tokens_spent: int
    wall_clock_s: float
    history: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""


# ── Direct sandbox operations (bypassing LLM / worker) ─────────────────────────
async def _write_file(path: str, content: str, *, session_id: str, user_id: str) -> None:
    from core.sandbox import get_sandbox_provider

    try:
        await get_sandbox_provider().put_file(session_id, path, content, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[loop] write_file %s failed: %s", path, exc)


async def _read_file(path: str, *, session_id: str, user_id: str) -> str:
    from core.sandbox import get_sandbox_provider

    try:
        data = await get_sandbox_provider().get_file(session_id, path, user_id=user_id)
        return bytes(data).decode("utf-8", "replace") if data else ""
    except Exception:  # noqa: BLE001 - file missing etc.; treat as empty
        return ""


async def _sbx_exec(
    cmd: str, *, session_id: str, user_id: str, timeout: int = 60
) -> tuple[int, str, str]:
    """Run a bash snippet in the persistent sandbox (used for git checkpoints); returns (exit_code, stdout, stderr)."""
    from core.sandbox import (
        ExecuteRequest,
        SandboxConnectError,
        SandboxError,
        SandboxTimeoutError,
        get_sandbox_provider,
    )

    req = ExecuteRequest(
        script_content=cmd, script_name="_loop_git.sh", language="bash",
        timeout=max(1, min(int(timeout), 300)), session_id=session_id, user_id=user_id,
    )
    try:
        res = await get_sandbox_provider().execute(req)
        return res.exit_code, res.stdout or "", res.stderr or ""
    except (SandboxTimeoutError, SandboxConnectError, SandboxError) as exc:
        logger.warning("[loop] sbx_exec failed: %s", exc)
        return -1, "", str(exc)


# ── git checkpoints (best-effort: no git / failures degrade gracefully, never take down the loop) ─────────────
async def _git_init(session_id: str, user_id: str) -> bool:
    code, _, _ = await _sbx_exec(
        f"cd {_WORKSPACE} 2>/dev/null && command -v git >/dev/null 2>&1 || exit 42; "
        f"git rev-parse --git-dir >/dev/null 2>&1 && exit 0; "
        "git init -q && git config user.email loop@agent.local && "
        "git config user.name loop && git add -A >/dev/null 2>&1; "
        "git commit -q -m baseline --allow-empty >/dev/null 2>&1 || true",
        session_id=session_id, user_id=user_id,
    )
    return code == 0


async def _git_checkpoint(session_id: str, user_id: str, msg: str) -> Optional[str]:
    """Commit a known-good checkpoint; returns the commit sha (None on failure / no git)."""
    code, out, _ = await _sbx_exec(
        f"cd {_WORKSPACE} && git rev-parse --git-dir >/dev/null 2>&1 || exit 42; "
        f"git add -A >/dev/null 2>&1; "
        f"git commit -q -m {json.dumps(msg)} --allow-empty >/dev/null 2>&1; "
        "git rev-parse --short HEAD",
        session_id=session_id, user_id=user_id,
    )
    return out.strip() if code == 0 and out.strip() else None


async def _git_worktree_changed(session_id: str, user_id: str) -> bool:
    """本轮工作区相对上个 checkpoint 是否真有改动（机检失败轮的客观推进信号）。"""
    code, out, _ = await _sbx_exec(
        f"cd {_WORKSPACE} && git rev-parse --git-dir >/dev/null 2>&1 || exit 42; "
        "git status --porcelain 2>/dev/null | head -1",
        session_id=session_id, user_id=user_id,
    )
    return code == 0 and bool(out.strip())


async def _git_diff_stat(session_id: str, user_id: str) -> str:
    code, out, _ = await _sbx_exec(
        f"cd {_WORKSPACE} && git rev-parse --git-dir >/dev/null 2>&1 || exit 42; "
        "git diff HEAD~1 HEAD --stat 2>/dev/null | tail -20",
        session_id=session_id, user_id=user_id,
    )
    return out.strip() if code == 0 else ""


# ── Requirement ledger feature_list.json (driver-owned; worker may not delete or modify) ───────────────────
def _init_req_fields(requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """补齐需求条目的运行时字段（新建账本与重规划后的新条目共用）。"""
    for r in requirements:
        r.setdefault("passes", False)
        r.setdefault("evidence", "")
        r.setdefault("attempts", 0)
        r.setdefault("stalls", 0)
        r.setdefault("blocked", False)
        r.setdefault("check_cmd", "")
        r.setdefault("last_feedback", "")
    return requirements


def _new_ledger(objective: str, requirements: List[Dict[str, Any]]) -> Dict[str, Any]:
    # 字段语义：attempts=总尝试轮数（防失控硬上限）；stalls=连续无实质推进轮数
    # （停滞判定与停滞告警都看它）；blocked=搁置；check_cmd=只读机检命令（driver
    # 亲自执行，exit 0=客观达标，空=纯语义需求）；last_feedback=本需求最近一次
    # 评审/机检反馈（重规划输入；不跨需求泄漏）。
    return {"objective": objective, "iteration": 0,
            "requirements": _init_req_fields(requirements)}


async def _read_ledger(
    *, session_id: str, user_id: str, loop_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Read the sandbox ledger; reject one stamped by a different loop.

    Sandboxes are pool-recycled across sessions of the same user (idle
    sessions park their sandbox for reuse), so a brand-new loop can inherit
    the previous loop's /workspace — including its feature_list.json.
    Observed live: a fresh loop "resumed" from a dead loop's checkpoint with
    a requirement already attempt-blocked. The loop_id stamp (written by
    _write_ledger) makes foreign ledgers invisible; unstamped legacy ledgers
    are still accepted.
    """
    raw = await _read_file(_LEDGER_PATH, session_id=session_id, user_id=user_id)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not (isinstance(obj, dict) and obj.get("requirements")):
        return None
    stamp = obj.get("loop_id")
    if loop_id and stamp != loop_id:
        # Unstamped counts as foreign too: a recycled sandbox carrying a
        # pre-stamp ledger is exactly the contamination case. The DB mirror
        # (keyed by loop_id) is the compatibility path for legacy resumes.
        logger.info(
            "[loop %s] ignoring foreign sandbox ledger (stamped %s — recycled sandbox)",
            loop_id, stamp or "<none>",
        )
        return None
    return obj


async def _write_ledger(ledger: Dict[str, Any], *, session_id: str, user_id: str) -> None:
    # Each iteration the driver overwrites with its own authoritative copy → any worker edits to the ledger are discarded (tamper-proofing).
    await _write_file(
        _LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=2),
        session_id=session_id, user_id=user_id,
    )


def _next_requirement(ledger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Skip requirements that already passed or are blocked (attempt cap exhausted); take the next pending one.
    for r in ledger["requirements"]:
        if not r.get("passes") and not r.get("blocked"):
            return r
    return None


def _has_blocked(ledger: Dict[str, Any]) -> bool:
    return any(r.get("blocked") and not r.get("passes") for r in ledger["requirements"])


def _all_pass(ledger: Dict[str, Any]) -> bool:
    return all(r.get("passes") for r in ledger["requirements"])


def _pick_fresher_ledger(
    db_led: Optional[Dict[str, Any]], sbx_led: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Between the DB ledger (reliable across rebuild/restart/machine change) and the sandbox
    ledger (freshest within a same-process live session), pick the one with the higher iteration
    count; ties go to the DB (authoritative source of truth). If either is empty, return the other."""
    if db_led and sbx_led:
        di = int(db_led.get("iteration", 0) or 0)
        si = int(sbx_led.get("iteration", 0) or 0)
        return sbx_led if si > di else db_led
    return db_led or sbx_led


def _progress_frac(ledger: Dict[str, Any]) -> str:
    reqs = ledger["requirements"]
    done = sum(1 for r in reqs if r.get("passes"))
    return f"{done}/{len(reqs)}"


def _render_ledger_view(ledger: Dict[str, Any], *, current_id: str) -> str:
    lines = []
    for r in ledger["requirements"]:
        mark = "x" if r.get("passes") else " "
        cur = " ← 本轮" if r["id"] == current_id else ""
        lines.append(f"- [{mark}] {r['id']}: {r['description']}{cur}")
    return "\n".join(lines)


# ── One worker iteration (fresh context, same persistent sandbox) ────────────────
async def _run_worker_iteration(
    *,
    prompt: str,
    session_id: str,
    user_id: str,
    model_name: Optional[str],
    model_provider_id: Optional[str] = None,
    worker_max_iters: int,
    enable_thinking: bool,
    chat_mode: Optional[str],
    emit: Optional[EmitFn],
    is_cancelled: Optional[CancelFn],
    project_ctx: Optional[Dict[str, Any]] = None,
    chat_id: Optional[str] = None,
    ontology_enabled: bool = False,
    ontology_runtime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a brand-new tools-enabled agent (bound to the persistent sandbox); returns {text, tokens, tool_calls}.

    When ``project_ctx`` / ``chat_id`` are given, the worker's file/myspace tools are scoped to the
    user-selected **project folder** (where the site source lives) — changes land in the real
    project and publish_site can locate the site by conversation, no longer an isolated draft.
    """
    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients
    from orchestration.streaming import StreamingAgent

    agent, clients = await create_agent_executor(
        current_user_id=user_id,
        model_name=model_name,
        model_provider_id=model_provider_id,  # 用户在会话里选定的模型跟随 loop（不再固定默认模型）
        sandbox_session_id=session_id,  # key: same session → files persist across iterations
        project_ctx=project_ctx,        # key: bind to the user-selected project (site source workspace)
        chat_id=chat_id,
        isolated=True,  # independent MCP clients per iteration, avoids cross-task cancel-scope
        max_iters=worker_max_iters,
        ontology_runtime=ontology_runtime,
        # Tight per-tool-result context cap: loop workers grep/read huge draft
        # files every round, and the ReAct loop re-sends all accumulated tool
        # results each round — the default 20k cap measured 650k–1M tokens PER
        # ITERATION on the 200-page-report workload. Full content stays readable
        # via /workspace/.offload. Env-tunable.
        tool_result_limit=_loop_tool_result_limit(),
    )
    sa = StreamingAgent(agent, clients)
    text = ""
    tool_calls = 0
    trace: List[Dict[str, Any]] = []
    citations: List[Dict[str, Any]] = []
    # 证据锚点：每轮 worker 一个独立发号器，绑到 agent 上与中间件共享
    from orchestration.citation_anchor import AnchorAllocator, attach_allocator

    _anchor_allocator = AnchorAllocator()
    attach_allocator(agent, _anchor_allocator)
    from core.ontology.validator import requires_output_review

    runtime = ontology_runtime or {"enabled": False, "packs": [], "review_level": "none"}
    hold_output = requires_output_review(runtime)

    # Keep the sandbox session alive for the whole worker phase — see
    # core/sandbox/keepalive.py for why (idle reaper vs long model-streaming
    # phases with no sandbox calls).
    from core.sandbox.keepalive import start_session_keepalive

    _keepalive_task = start_session_keepalive(session_id)
    try:
        async for et, payload in sa.stream(
            [{"role": "user", "content": prompt}],
            {
                "user_id": user_id,
                "model_name": model_name or "",
                "enable_thinking": enable_thinking,
                # Pass the thinking level through verbatim: apply_request_context → AgentRuntimeState.chat_mode
                # → _resolve_chat_mode → reasoning_effort. If omitted, the middleware falls back on
                # enable_thinking (True=medium/False=fast) and the "high/extreme" levels would be lost.
                "chat_mode": (chat_mode or "").lower(),
                "ontology_enabled": ontology_enabled,
                "ontology_runtime": runtime,
            },
        ):
            if is_cancelled and is_cancelled():
                break
            # Forward the worker's per-iteration streaming events in the **same SSE format as the
            # main conversation**, so the frontend renders them through the same pipeline as a normal
            # chat (content/tool_call/tool_result) rather than as an "iteration N" block.
            if et == "text_delta":
                text += payload
                if emit and not hold_output:
                    await emit({"type": "content", "event": "ai_message", "delta": payload})
            elif et == "reasoning_protocol":
                if emit:
                    await emit({"type": "thinking", **payload})
            elif et == "thinking_delta":
                if emit:
                    await emit({"type": "thinking", "delta": payload})
            elif et == "tool_call_start":
                if emit:
                    await emit(
                        {
                            "type": "tool_call_start",
                            "tool_name": payload.get("name"),
                            "tool_id": payload.get("id"),
                        }
                    )
            elif et == "tool_call_delta":
                if emit and payload.get("delta"):
                    await emit(
                        {
                            "type": "tool_call_delta",
                            "tool_name": payload.get("name"),
                            "tool_id": payload.get("id"),
                            "arguments_delta": payload.get("delta"),
                        }
                    )
            elif et == "tool_call":
                tool_calls += 1
                trace.append(
                    {
                        "type": "tool_call",
                        "tool_name": payload.get("name"),
                        "tool_id": payload.get("id"),
                        "tool_args": payload.get("args"),
                    }
                )
                if emit:
                    await emit({"type": "tool_call", "tool_name": payload.get("name"),
                                "tool_id": payload.get("id"), "tool_args": payload.get("args")})
            elif et == "tool_result":
                tool_name = payload.get("name")
                tool_id = payload.get("id")
                tool_content = payload.get("content")
                trace.append(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "tool_id": tool_id,
                        "result": tool_content,
                    }
                )
                try:
                    parsed_result = (
                        json.loads(tool_content)
                        if isinstance(tool_content, str)
                        else tool_content
                    )
                    if isinstance(parsed_result, dict):
                        from orchestration.citation_anchor import collect_citation_dicts

                        citations.extend(
                            collect_citation_dicts(tool_id or "", _anchor_allocator)
                        )
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                if emit:
                    await emit({"type": "tool_result", "tool_name": payload.get("name"),
                                "tool_id": payload.get("id"), "result": payload.get("content")})
            elif et == "tool_pending":
                if emit:
                    await emit({"type": "tool_pending", **(payload or {})})
            elif et == "error":
                logger.warning("[loop] worker stream error: %s", payload)
    finally:
        _keepalive_task.cancel()
        usage = sa.get_usage()
        await close_clients(clients)
    if hold_output and text:
        from orchestration.subagents.ontology_reviewer import review_ontology_output

        if emit:
            await emit(
                {
                    "type": "ontology_review",
                    "status": "started",
                    "level": runtime.get("review_level", "checkpoint"),
                }
            )
        review = await review_ontology_output(
            task=prompt,
            answer=text,
            runtime=runtime,
            trace=trace,
            citations=citations,
            user_id=user_id,
            chat_id=chat_id,
            model_name=model_name,
        )
        text = review["answer"]
        if emit:
            await emit(
                {
                    "type": "ontology_review",
                    "status": "completed",
                    "level": runtime.get("review_level", "checkpoint"),
                    "verdict": review["verdict"],
                }
            )
            await emit({"type": "content", "event": "ai_message", "delta": text})
    return {
        "text": text,
        "tokens": int(usage.get("total_tokens", 0)),
        "tool_calls": tool_calls,
    }


def _build_requirement_prompt(
    *,
    objective: str,
    ledger: Dict[str, Any],
    req: Dict[str, Any],
    seq: int,
    handoff: str,
    feedback: str,
    strategy_change: bool,
    in_project: bool,
    steering: Optional[List[str]] = None,
) -> str:
    """One requirement at a time: feed only the current requirement to the worker (Claude Code does one feature at a time)."""
    if in_project:
        workspace_note = (
            "\n## 工作区（当前项目文件夹）\n你已绑定到用户在输入框选定的**项目**——站点/前端"
            "工程的真实源码就在这个项目文件夹里。开工先列目录、读相关文件，在**已有源码**上继续改，"
            "**不要**从零重写、也不要把产出写到 /workspace 临时区（那样改动不会落到项目里）。"
        )
    else:
        workspace_note = (
            "\n## 持久工作区\n你的文件保存在沙箱 /workspace，**跨迭代持久**且已是 git 仓库。"
            "开工先 `ls -la /workspace` 并读相关文件，在已有成果上继续，不要从零重写。"
        )
    workspace_note += (
        "\n⚠️ 大文件纪律：超过 3 万字符的文件**禁止整读进上下文**（读了也会被截断，"
        "还白白吃掉预算）。核验/定位一律用统计与抽样命令：`wc -m`、`grep -n/-c`、"
        "`head`/`tail`/`sed -n 'a,bp'`、`word-cli read --mode outline`；要改哪段就只读哪段。"
        "被截断的工具结果全文都在 /workspace/.offload/ 里，需要时按路径查。"
    )
    parts = [
        f"# 自主任务（第 {seq} 轮 · 需求 {req['id']} · 总进度 {_progress_frac(ledger)}）",
        f"\n## 总目标\n{objective}",
        (
            "\n## 需求账本（只读，你**无权**修改 feature_list.json）\n"
            + _render_ledger_view(ledger, current_id=req["id"])
        ),
        (
            f"\n## 🎯 本轮唯一目标：完成需求 {req['id']}\n{req['description']}\n\n"
            "**只做这一条**。不要提前做别的需求、不要改需求账本——把这一条扎实做到位、"
            "落进真实文件（会有一个独立评审员打开你产出的文件逐条核验，光声称做了没用）。"
            + (
                f"\n本需求还有一条机检命令（driver 会亲自执行，退出码 0 才算数）：\n"
                f"`{req['check_cmd']}`\n完工前自己先跑一遍确认能过。"
                if req.get("check_cmd")
                else ""
            )
        ),
        workspace_note,
    ]
    if steering:
        parts.append(
            "\n## 📣 用户临时指令（最高优先级，本轮必须遵循）\n"
            + "\n".join(f"- {s}" for s in steering)
        )
    if handoff:
        parts.append(f"\n## 上一轮交接（git diff + 摘要）\n{handoff}")
    if feedback:
        parts.append(
            "\n## 评审反馈（评审员亲自看了你上轮的真实产出，务必针对性改进）\n" + feedback
        )
    if strategy_change:
        parts.append(
            "\n## ⚠️ 停滞告警（自我修正）\n这条需求已连续多轮没通过评审。**不要再沿用同一思路**，"
            "本轮请**换一个根本不同的方法/实现/结构**重做，并简述为什么新思路能突破瓶颈。"
        )
    if in_project:
        parts.append(
            "\n## 收尾\n完成后自检产出确已写进项目文件。若本项目是一个站点/前端工程且你改动了它，"
            "**务必调用 publish_site 发布新版**（带上现有 site_id 发新版），否则线上站点不会更新。"
        )
    else:
        parts.append(
            "\n## 收尾\n完成后自行用 bash 快速自检，确认本需求确已扎实落地。只做这一条能扎实完成的部分。"
        )
    return "\n".join(parts)


async def _make_handoff(worker_text: str, evidence: str, git_diff: str) -> str:
    """Compressed handoff: this iteration's worker output + environment evidence + git diff summary → a short handoff for the next iteration."""
    summary = worker_text.strip()
    if len(summary) > 1000:
        summary = summary[:500] + "\n...\n" + summary[-400:]
    ev = evidence.strip()[:500]
    out = f"【本轮工作】\n{summary}\n\n【环境验证】\n{ev}"
    if git_diff:
        out += f"\n\n【本轮改动 git diff --stat】\n{git_diff}"
    return out


async def _emit(emit: Optional[EmitFn], event: Dict[str, Any]) -> None:
    if emit:
        try:
            await emit(event)
        except Exception:  # noqa: BLE001
            pass


async def run_autonomous_loop(
    *,
    loop_id: str,
    user_id: str,
    goal_spec: GoalSpec,
    budget: LoopBudget,
    model_name: Optional[str] = None,
    model_provider_id: Optional[str] = None,
    evaluator_model: Optional[str] = None,
    worker_max_iters: int = 15,
    session_id: Optional[str] = None,
    hitl_enabled: bool = False,
    enable_thinking: bool = False,
    chat_mode: Optional[str] = None,
    emit: Optional[EmitFn] = None,
    is_cancelled: Optional[CancelFn] = None,
    load_ledger: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    save_ledger: Optional[Callable[[Dict[str, Any]], None]] = None,
    poll_steering: Optional[SteeringFn] = None,
    project_ctx: Optional[Dict[str, Any]] = None,
    chat_id: Optional[str] = None,
    tenant_id: str = "default",
) -> LoopResult:
    """Drive an autonomous loop until "all requirements in the ledger pass" / budget exhausted / cancelled.

    Callable directly (tests/CLI), or wrapped into a ChatRun background run by
    chat_run_executor's _run_autonomous_loop_workflow. ``emit`` posts events to the SSE stream;
    ``is_cancelled`` is a cooperative polling terminator.

    ``load_ledger`` / ``save_ledger``: optional DB-mirror callbacks for the requirement ledger.
    When given, every ledger flush is mirrored into the DB, and resume prefers restoring from the
    DB — no longer dependent on whether the sandbox /workspace still exists (rebuild/restart/
    machine change/unflushed snapshot all wipe the sandbox). When absent, degrades to a
    sandbox-only ledger (tests/CLI).

    ``project_ctx`` / ``chat_id``: the project context the user selected in the input box. When
    given, both the worker and the reviewer sub-agent are bound to that project folder (where the
    site source lives) — the worker edits the real source, the reviewer reads the real output, and
    publishing goes through publish_site. When absent, degrades to the isolated sandbox /workspace
    (pure task-style loop / tests).
    """
    session = session_id or f"loop-{loop_id}"
    from core.services.ontology_service import build_user_ontology_runtime

    # Resolved here, once per run, from the tenant's active profile. Held in a
    # local rather than a module constant so a concurrent run under a different
    # tenant cannot read this one's numbers.
    policy = _resolve_loop_policy(tenant_id=tenant_id, user_id=user_id)
    strategy_change_after = policy.strategy_change_after
    max_attempts_per_req = policy.max_attempts_per_requirement
    logger.info(
        "[loop %s] policy=%s tenant=%s change_after=%d max_attempts=%d budget_x%.2f",
        loop_id, policy.version, tenant_id, strategy_change_after,
        max_attempts_per_req, policy.budget_multiplier,
    )

    ontology_enabled, ontology_runtime = build_user_ontology_runtime(
        user_id=user_id,
        task=goal_spec.objective,
    )
    in_project = bool(project_ctx)
    t0 = time.monotonic()
    history: List[Dict[str, Any]] = []
    handoff = ""
    feedback = ""
    tokens_spent = 0
    seq = 0
    consecutive_infra = 0  # 连续零产出/异常轮计数（熔断用），健康轮清零
    final_score: Optional[float] = None
    # Acceptance criteria: fed to the reviewer sub-agent to verify the real output item by item (extracted once before the run, stored in the ledger, reused on resume).
    criteria: List[str] = list(goal_spec.acceptance_criteria or [])

    async def _persist_ledger(led: Dict[str, Any]) -> None:
        """Flush the ledger: dual-write to the sandbox (working cache) + DB mirror (reliable source of truth for resume)."""
        led["loop_id"] = loop_id  # stamp: _read_ledger rejects foreign ledgers on recycled sandboxes
        await _write_ledger(led, session_id=session, user_id=user_id)
        if save_ledger:
            try:
                save_ledger(led)
            except Exception as exc:  # noqa: BLE001 - a DB mirror failure must not take down the loop
                logger.warning("[loop %s] DB ledger mirror failed: %s", loop_id, exc)

    await _emit(emit, {
        "type": "loop_started", "loop_id": loop_id,
        "objective": goal_spec.objective, "budget": budget.snapshot(),
    })

    # ── Initializer / resume-from-checkpoint ─────────────────────────────────
    # Resume source-of-truth priority: DB-mirrored ledger (reliable across rebuild/restart/machine
    # change) > sandbox feature_list.json (freshest within a same-process live session). Take the
    # one with the higher iteration count; only when neither exists do the one-time objective decomposition.
    db_ledger: Optional[Dict[str, Any]] = None
    if load_ledger:
        try:
            db_ledger = load_ledger()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[loop %s] DB ledger load failed: %s", loop_id, exc)
    sbx_ledger = await _read_ledger(session_id=session, user_id=user_id, loop_id=loop_id)
    ledger = _pick_fresher_ledger(db_ledger, sbx_ledger)
    if ledger is not None:
        ledger["loop_id"] = loop_id  # stamp before any backfill (DB mirrors from older builds lack it)
        seq = int(ledger.get("iteration", 0) or 0)
        # Sandbox ledger missing/stale (sandbox wiped after restart or snapshot not flushed) →
        # backfill the authoritative ledger into the sandbox so the worker can read feature_list.json.
        if ledger is not sbx_ledger:
            await _write_ledger(ledger, session_id=session, user_id=user_id)
        handoff = await _read_file(f"{_WORKSPACE}/handoffs.md", session_id=session, user_id=user_id)
        logger.info("[loop %s] RESUME from iter %d, progress %s (src=%s)", loop_id, seq,
                    _progress_frac(ledger), "db" if ledger is db_ledger else "sandbox")
        await _emit(emit, {"type": "loop_resumed", "from_iteration": seq,
                           "progress": _progress_frac(ledger)})
        # On resume, also send the ledger to the frontend so the "plan bar" can repopulate (the init branch won't run again).
        await _emit(emit, {"type": "loop_plan", "objective": goal_spec.objective,
                           "requirements": [
                               {"id": r["id"], "description": r["description"],
                                "passes": bool(r.get("passes"))}
                               for r in ledger["requirements"]]})
    else:
        await _git_init(session, user_id)
        # 规划器 v2：只读侦察员先摸真实工作区/项目 → 规划模型据实拆账本（含可选机检命令）。
        # 侦察/规划任一环节失败都退回旧 decompose 链路，绝不因规划升级而拖垮循环。
        survey = ""
        try:
            survey = await scout_workspace(
                objective=goal_spec.objective, session_id=session, user_id=user_id,
                project_ctx=project_ctx, chat_id=chat_id, model_name=evaluator_model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[loop %s] scout failed: %s", loop_id, exc)
        if survey:
            await _emit(emit, {"type": "loop_scouted", "survey": survey[:800]})
        reqs = await plan_requirements(
            goal_spec=goal_spec, survey=survey, model_name=evaluator_model, user_id=user_id,
        )
        if not reqs:
            reqs = await decompose_requirements(
                goal_spec=goal_spec, model_name=evaluator_model or "fast", user_id=user_id,
            )
        ledger = _new_ledger(goal_spec.objective, reqs)
        if survey:
            ledger["survey"] = survey  # 存档给重规划用（重拆时无需再侦察一遍）
        await _persist_ledger(ledger)
        await _write_file(
            f"{_WORKSPACE}/PROGRESS.md",
            f"# 目标\n{goal_spec.objective}\n\n# 需求账本\n"
            + _render_ledger_view(ledger, current_id="") + "\n",
            session_id=session, user_id=user_id,
        )
        await _git_checkpoint(session, user_id, "loop: init feature_list")
        await _emit(emit, {
            "type": "loop_plan", "objective": goal_spec.objective,
            "requirements": [
                {"id": r["id"], "description": r["description"], "passes": bool(r.get("passes"))}
                for r in ledger["requirements"]
            ],
        })
        logger.info("[loop %s] init ledger with %d requirements", loop_id, len(ledger["requirements"]))

    # Acceptance-criteria resolution (for the reviewer sub-agent's item-by-item verification): reuse from the ledger if present (resume), otherwise extract once and store back into the ledger.
    if not criteria:
        criteria = list(ledger.get("criteria") or [])
    if not criteria:
        criteria = await extract_acceptance_criteria(
            objective=goal_spec.objective, model_name=evaluator_model or "fast", user_id=user_id,
        ) or [goal_spec.objective]
        ledger["criteria"] = criteria
        await _persist_ledger(ledger)

    def _budget_left() -> Optional[str]:
        # 预算是可选约束：<=0 一律不限（默认）。「能完成任务」优先于省预算——
        # 唯一的无条件上限是防死循环的硬后备 LOOP_HARD_MAX_ITERS。
        if seq >= _hard_max_iters():
            return f"触发防失控硬后备（{_hard_max_iters()} 轮）——请检查任务是否根本无法收敛"
        if budget.max_iters > 0 and seq >= budget.max_iters:
            return f"达到最大迭代数 {budget.max_iters}"
        if budget.max_wall_clock_s > 0 and time.monotonic() - t0 >= budget.max_wall_clock_s:
            return f"达到最大墙钟 {budget.max_wall_clock_s}s"
        if budget.max_tokens > 0 and tokens_spent >= budget.max_tokens:
            return f"达到 token 预算 {budget.max_tokens}"
        return None

    status = "running"
    reason = ""

    while True:
        if is_cancelled and is_cancelled():
            status, reason = "cancelled", "外部取消"
            break

        # The stop gate takes precedence over the budget: no next pending requirement → wrap up
        # (the second-pass re-check was already done per item at flip time). This must be checked
        # first — otherwise "the last item flipping exactly on the budget-cap iteration" would be
        # misreported as budget_exhausted.
        req = _next_requirement(ledger)
        if req is None:
            if _has_blocked(ledger):
                # Some requirements exhausted their attempts without passing (and not HITL) → partially done; report truthfully, never falsely claim success.
                status = "budget_exhausted"
                reason = f"部分需求多轮未通过评审（{_progress_frac(ledger)}）"
            else:
                status = "completed"
                reason = f"需求账本全部通过（{_progress_frac(ledger)}）"
            break

        exhausted = _budget_left()
        if exhausted:
            status, reason = "budget_exhausted", exhausted
            break

        seq += 1
        ledger["iteration"] = seq
        # 用户临时指令（steer）：每轮开工前取一次队列，注入本轮 worker prompt（最高优先级）。
        steering: List[str] = []
        if poll_steering:
            try:
                steering = [s for s in (poll_steering() or []) if str(s).strip()]
            except Exception as exc:  # noqa: BLE001 - steering 读取失败不拖垮循环
                logger.warning("[loop %s] poll_steering failed: %s", loop_id, exc)
        if steering:
            await _emit(emit, {"type": "loop_steering_consumed", "seq": seq,
                               "messages": [s[:200] for s in steering]})
        await _emit(emit, {"type": "iteration_started", "seq": seq,
                           "requirement_id": req["id"], "progress": _progress_frac(ledger)})
        logger.info("[loop %s] iter %d req=%s (%s)", loop_id, seq, req["id"], _progress_frac(ledger))

        # 1) Worker runs one iteration (fresh context, fed only the current requirement)
        # 停滞告警看 stalls（连续无实质推进轮数）而非 attempts：健康推进多轮的大需求
        # （如 20 章正文逐章写）不能每轮被怂恿「换根本不同的方法」推翻半成品——
        # 这与 2026-08-10 的 progress/stall 修复必须同一口径。
        strategy_change = int(req.get("stalls", 0)) >= strategy_change_after
        prompt = _build_requirement_prompt(
            objective=goal_spec.objective, ledger=ledger, req=req, seq=seq,
            handoff=handoff, feedback=feedback, strategy_change=strategy_change,
            in_project=in_project, steering=steering,
        )
        # Per-iteration 异常隔离：worker 内任何未被流层吞掉的异常（网关断流抛错、
        # AgentScope 内部错、沙箱协议外异常）只废掉**本轮**，绝不冒泡杀死整个多小时
        # run。异常轮与零产出轮同待遇：不计 attempt、退避重试；连续多轮才熔断。
        try:
            work = await _run_worker_iteration(
                prompt=prompt, session_id=session, user_id=user_id, model_name=model_name,
                model_provider_id=model_provider_id,
                worker_max_iters=worker_max_iters, enable_thinking=enable_thinking,
                chat_mode=chat_mode, emit=emit, is_cancelled=is_cancelled,
                project_ctx=project_ctx, chat_id=chat_id,
                ontology_enabled=ontology_enabled,
                ontology_runtime=ontology_runtime,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[loop %s] iter %d worker raised: %s", loop_id, seq, exc, exc_info=True)
            work = {"text": "", "tokens": 0, "tool_calls": 0, "_infra_error": str(exc)[:200]}
        tokens_spent += work["tokens"]
        if is_cancelled and is_cancelled():
            status, reason = "cancelled", "外部取消"
            break

        # Infrastructure-failure guard: a worker round that produced NOTHING
        # (no tool calls, no text, no tokens) — or raised — is an environment
        # outage, not evidence about the requirement. Not counted as an
        # attempt/stall; back off and retry. A permanent outage is bounded by
        # the consecutive-infra circuit breaker (LOOP_MAX_CONSECUTIVE_INFRA).
        if not work["tokens"] and not work["tool_calls"] and not str(work.get("text") or "").strip():
            consecutive_infra += 1
            if consecutive_infra >= _max_consecutive_infra():
                status = "failed"
                reason = (
                    f"连续 {consecutive_infra} 轮零产出/异常（疑似模型网关或沙箱持续故障），"
                    "已熔断。排除环境故障后可「继续」从断点续跑。"
                )
                break
            logger.warning(
                "[loop %s] iter %d req=%s produced nothing (infra failure %d/%d) — "
                "not counted as attempt; backing off 30s",
                loop_id, seq, req["id"], consecutive_infra, _max_consecutive_infra(),
            )
            await _emit(emit, {"type": "iteration_evaluated", "seq": seq,
                               "requirement_id": req["id"], "verdict": "infra_retry",
                               "tool_calls": 0, "tokens": 0,
                               "reason": "本轮无任何产出（疑似模型/网关故障），不计入尝试，稍后重试"
                                         + (f"：{work.get('_infra_error')}" if work.get("_infra_error") else "")})
            await asyncio.sleep(30)
            continue

        consecutive_infra = 0
        req["attempts"] = int(req.get("attempts", 0)) + 1

        # 2) 混合验收（Codex「退出码是金标准」+ 语义评审兜底）：
        #    a. 需求带机检命令 → driver **亲自**在沙箱执行（worker 无法作弊）。
        #       机检失败 → 直接反馈命令输出、不烧一次评审 agent（省一整个评审员运行）；
        #       推进信号退化为「工作区是否真有新改动」。
        #    b. 机检通过/无机检 → 只读评审员亲验真实产出（never trust self-report）。
        check_cmd = str(req.get("check_cmd") or "").strip()
        check_passed: Optional[bool] = None
        machine_evidence = ""
        if check_cmd:
            _code, _out, _err = await _sbx_exec(
                check_cmd, session_id=session, user_id=user_id, timeout=90,
            )
            check_passed = _code == 0
            _check_tail = (_out or _err or "").strip()[-500:]
            machine_evidence = (
                f"driver 已执行机检命令 `{check_cmd}`，退出码 {_code}"
                + (f"，输出尾部：{_check_tail}" if _check_tail else "")
            )
            await _emit(emit, {"type": "loop_check", "seq": seq, "requirement_id": req["id"],
                               "cmd": check_cmd, "exit_code": _code, "passed": check_passed})

        if check_passed is False:
            progressed = await _git_worktree_changed(session, user_id)
            review = {
                "verdict": CONTINUE,
                "criteria_hit": [],
                "evidence": machine_evidence,
                "progress": progressed,
                "feedback": f"机检未通过：{machine_evidence}。修复到该命令退出码为 0 再收工。",
            }
        else:
            review = await review_requirement(
                objective=goal_spec.objective, requirement_desc=req["description"],
                acceptance_criteria=criteria, worker_summary=work["text"],
                machine_evidence=machine_evidence,
                session_id=session, user_id=user_id,
                project_ctx=project_ctx, chat_id=chat_id,
                model_name=evaluator_model or model_name,
                requirement_id=req["id"], emit=emit,
            )
        verdict = review.get("verdict")
        evidence = review.get("evidence", "")

        rec = {
            "seq": seq, "requirement_id": req["id"], "verdict": verdict,
            "tool_calls": work["tool_calls"], "tokens": work["tokens"],
            "reason": review.get("feedback", ""), "decided_by": "reviewer",
        }
        history.append(rec)
        await _emit(emit, {"type": "iteration_evaluated", **rec, "evidence": evidence[:600]})
        logger.info("[loop %s] iter %d req=%s verdict=%s (attempt %d)",
                    loop_id, seq, req["id"], verdict, req["attempts"])

        # 3) Decide the flip (passes: false→true); only the driver may flip.
        #    二次复核降频：机检已过（有客观退出码佐证）且不是收官需求 → 一次语义评审即可翻牌；
        #    纯语义需求、或翻牌即整环完成的收官需求 → 仍加独立二次复核（防提前收工）。
        passed = False
        if verdict == DONE:
            flip_completes = all(
                r.get("passes") or r.get("blocked") or r["id"] == req["id"]
                for r in ledger["requirements"]
            )
            need_confirm = (check_passed is not True) or flip_completes
            if not need_confirm:
                passed = True
            else:
                confirm = await review_requirement(
                    objective=goal_spec.objective, requirement_desc=req["description"],
                    acceptance_criteria=criteria, worker_summary=work["text"],
                    machine_evidence=machine_evidence,
                    session_id=session, user_id=user_id,
                    project_ctx=project_ctx, chat_id=chat_id,
                    model_name=evaluator_model or model_name, second_pass=True,
                    requirement_id=req["id"], emit=emit,
                )
                if confirm.get("verdict") == DONE:
                    passed = True
                    evidence = confirm.get("evidence") or evidence
                else:
                    rec["reason"] += "（二次复核未通过，继续）"
                    logger.info("[loop %s] req %s done 被二次复核驳回", loop_id, req["id"])

        # 4) HITL: reviewer requests human confirmation (optional per-loop; CE default logs and continues)
        if not passed and verdict == NEED_HUMAN and hitl_enabled:
            await _persist_ledger(ledger)
            status, reason = "awaiting_human", review.get("feedback", "评审请求人工确认")
            await _emit(emit, {"type": "loop_awaiting_human", "seq": seq, "reason": reason})
            break

        # 5) Flip the item + git known-good checkpoint; otherwise feed the review feedback back into the next iteration.
        git_sha = None
        if passed:
            req["passes"] = True
            req["evidence"] = evidence[:800]
            git_sha = await _git_checkpoint(session, user_id, f"loop: {req['id']} passed")
            await _emit(emit, {"type": "requirement_passed", "requirement_id": req["id"],
                               "progress": _progress_frac(ledger), "commit": git_sha})
            logger.info("[loop %s] ✓ %s passed (%s) commit=%s",
                        loop_id, req["id"], _progress_frac(ledger), git_sha)
        else:
            # Stall bookkeeping: the reviewer affirms material progress (progress=true,
            # evidence-backed) → reset the streak; otherwise count one stalled round.
            # A mega-requirement legitimately spanning many rounds (e.g. 20-chapter
            # body written chapter-by-chapter) used to hit the flat attempt cap and
            # get blocked mid-progress — the 2026-08-10 HugAgentOS 200-page rerun lost
            # its core body requirement exactly this way at attempt 6 while the
            # draft had healthily grown every round.
            if bool(review.get("progress")):
                req["stalls"] = 0
            else:
                req["stalls"] = int(req.get("stalls", 0)) + 1
            # Stagnation exit: consecutive no-progress rounds at the cap, or total
            # attempts at the hard ceiling (progress affirmations must not defeat
            # the anti-infinite-loop guarantee) → HITL suspend / otherwise mark
            # blocked and skip.
            _stalled = int(req.get("stalls", 0)) >= max_attempts_per_req
            _runaway = int(req.get("attempts", 0)) >= max_attempts_per_req * 3
            if _stalled or _runaway:
                if hitl_enabled:
                    await _persist_ledger(ledger)
                    status = "awaiting_human"
                    reason = (
                        f"需求 {req['id']} 连续 {req['stalls']} 轮无实质推进，请人工介入"
                        if _stalled
                        else f"需求 {req['id']} 已尝试 {req['attempts']} 轮仍未通过评审，请人工介入"
                    )
                    await _emit(emit, {"type": "loop_awaiting_human", "seq": seq, "reason": reason})
                    break
                req["blocked"] = True
                req["last_feedback"] = str(review.get("feedback", "") or "")[:400]
                await _emit(emit, {"type": "loop_stagnation", "seq": seq,
                                   "requirement_id": req["id"], "attempts": req["attempts"],
                                   "stalls": req["stalls"]})
                logger.info("[loop %s] req %s blocked after %d attempts (%d consecutive stalls)",
                            loop_id, req["id"], req["attempts"], req["stalls"])
                # 重规划：需求被搁置说明原拆法/方向可能有问题——对剩余未完成部分重拆
                # （已通过项不动），而不是一条条 blocked 到只剩「部分完成」。次数有护栏。
                if int(ledger.get("replans", 0) or 0) < _max_replans():
                    new_reqs = await replan_remaining(
                        goal_spec=goal_spec, ledger=ledger,
                        survey=str(ledger.get("survey", "") or ""),
                        model_name=evaluator_model, user_id=user_id,
                    )
                    if new_reqs:
                        ledger["replans"] = int(ledger.get("replans", 0) or 0) + 1
                        ledger["requirements"] = _init_req_fields(new_reqs)
                        feedback = ""
                        handoff = ""
                        await _persist_ledger(ledger)
                        await _emit(emit, {"type": "loop_replanned", "seq": seq,
                                           "replans": ledger["replans"]})
                        await _emit(emit, {"type": "loop_plan", "objective": goal_spec.objective,
                                           "requirements": [
                                               {"id": r["id"], "description": r["description"],
                                                "passes": bool(r.get("passes"))}
                                               for r in ledger["requirements"]]})
                        logger.info("[loop %s] REPLANNED (#%d): %d requirements",
                                    loop_id, ledger["replans"], len(ledger["requirements"]))
                        continue

        # 评审反馈只属于**当前需求**：翻牌后清空，绝不把上一条需求的反馈当成下一条的
        # 「评审反馈」注入（历史缺陷：R1 通过后 R2 首轮带着 R1 的结论开工）。
        if passed:
            feedback = ""
        else:
            feedback = review.get("feedback", "")
            req["last_feedback"] = feedback[:400]

        # 6) Handoff + persist ledger/progress (for resume). git diff takes precedence over the text summary.
        git_diff = await _git_diff_stat(session, user_id)
        if passed:
            # 交接同理按需求隔离：下一条需求只需要知道「上一条已完成」+ 改动面，
            # 不需要上一条的工作细节自述。
            handoff = f"【上一需求 {req['id']} 已完成并通过评审】"
            if git_diff:
                handoff += f"\n【其改动 git diff --stat】\n{git_diff}"
        else:
            handoff = await _make_handoff(work["text"], evidence, git_diff)
        await asyncio.gather(
            _persist_ledger(ledger),
            _write_file(
                f"{_WORKSPACE}/handoffs.md",
                f"# 第 {seq} 轮交接（需求 {req['id']} · 进度 {_progress_frac(ledger)}）\n"
                f"verdict={verdict} passed={passed}\n\n{handoff}\n",
                session_id=session, user_id=user_id,
            ),
            _write_file(
                f"{_WORKSPACE}/PROGRESS.md",
                _render_progress(goal_spec, ledger, history),
                session_id=session, user_id=user_id,
            ),
        )

    # 收尾交付轮：部分完成收场（停滞搁置/显式预算耗尽/硬后备触发）时，用现有成果
    # 做一轮「尽力交付」——整合已完成部分、如实列出未竟事项，而不是把半成品散件直接
    # 甩给用户。取消/失败/等待人工不做（前两者用户要么不想要、要么环境坏了）。
    if status == "budget_exhausted" and _wrapup_enabled():
        _done_n = sum(1 for r in ledger["requirements"] if r.get("passes"))
        if _done_n and not (is_cancelled and is_cancelled()):
            await _emit(emit, {"type": "loop_wrapup_started",
                               "progress": _progress_frac(ledger)})
            try:
                wrap = await _run_worker_iteration(
                    prompt=_build_wrapup_prompt(goal_spec.objective, ledger, in_project),
                    session_id=session, user_id=user_id, model_name=model_name,
                    model_provider_id=model_provider_id,
                    worker_max_iters=max(6, worker_max_iters // 2),
                    enable_thinking=enable_thinking, chat_mode=chat_mode,
                    emit=emit, is_cancelled=is_cancelled,
                    project_ctx=project_ctx, chat_id=chat_id,
                    ontology_enabled=ontology_enabled,
                    ontology_runtime=ontology_runtime,
                )
                tokens_spent += wrap["tokens"]
            except Exception as exc:  # noqa: BLE001 - 收尾失败不改变终态
                logger.warning("[loop %s] wrapup iteration failed: %s", loop_id, exc)

    # Convergence/exit: flush the final ledger (sandbox + DB).
    await _persist_ledger(ledger)
    if final_score is None:
        # Multi-requirement tasks without a numeric score: use the pass ratio as final_score (0~1).
        reqs = ledger["requirements"]
        if reqs:
            final_score = round(sum(1 for r in reqs if r.get("passes")) / len(reqs), 4)

    wall = time.monotonic() - t0
    result = LoopResult(
        status=status, iterations=seq, final_score=final_score,
        tokens_spent=tokens_spent, wall_clock_s=round(wall, 1),
        history=history, reason=reason,
    )
    await _emit(emit, {
        "type": "loop_completed", "status": status, "iterations": seq,
        "final_score": final_score, "tokens_spent": tokens_spent,
        "wall_clock_s": result.wall_clock_s, "reason": reason,
        "progress": _progress_frac(ledger),
    })
    logger.info("[loop %s] DONE status=%s iters=%d progress=%s score=%s tokens=%d wall=%.1fs",
                loop_id, status, seq, _progress_frac(ledger), final_score, tokens_spent, wall)
    return result


def _render_progress(
    goal_spec: GoalSpec, ledger: Dict[str, Any], history: List[Dict[str, Any]]
) -> str:
    lines = [f"# 目标\n{goal_spec.objective}\n", "# 需求账本",
             _render_ledger_view(ledger, current_id=""), "\n# 迭代记录"]
    for r in history:
        lines.append(
            f"- 第 {r['seq']} 轮 [{r.get('requirement_id', '')}]: verdict={r['verdict']} "
            f"tools={r['tool_calls']} — {r['reason'][:100]}"
        )
    return "\n".join(lines) + "\n"
