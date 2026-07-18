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
    DONE,
    NEED_HUMAN,
    GoalSpec,
    decompose_requirements,
    extract_acceptance_criteria,
)
from orchestration.subagents.loop_reviewer import review_requirement

logger = get_logger(__name__)

EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]
CancelFn = Callable[[], bool]

# How many consecutive iterations a requirement can go without flipping → force the worker to change strategy (self-correction).
_STRATEGY_CHANGE_AFTER = 2
# Max attempts for a single requirement before the stagnation exit (HITL suspend / otherwise mark blocked and skip); prevents a single-item infinite loop.
_MAX_ATTEMPTS_PER_REQ = 6

_WORKSPACE = "/workspace"
_LEDGER_PATH = f"{_WORKSPACE}/feature_list.json"


@dataclass
class LoopBudget:
    max_iters: int = 50
    max_wall_clock_s: float = 6 * 3600.0
    max_tokens: int = 10_000_000
    max_subagents: int = 20  # reserved

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


async def _git_diff_stat(session_id: str, user_id: str) -> str:
    code, out, _ = await _sbx_exec(
        f"cd {_WORKSPACE} && git rev-parse --git-dir >/dev/null 2>&1 || exit 42; "
        "git diff HEAD~1 HEAD --stat 2>/dev/null | tail -20",
        session_id=session_id, user_id=user_id,
    )
    return out.strip() if code == 0 else ""


# ── Requirement ledger feature_list.json (driver-owned; worker may not delete or modify) ───────────────────
def _new_ledger(objective: str, requirements: List[Dict[str, Any]]) -> Dict[str, Any]:
    for r in requirements:
        r.setdefault("passes", False)
        r.setdefault("evidence", "")
        r.setdefault("attempts", 0)   # iterations attempted for this item (attempt cap → strategy change / blocked exit)
        r.setdefault("blocked", False)  # still failing at the attempt cap → mark skipped, avoids a single-item infinite loop
    return {"objective": objective, "iteration": 0, "requirements": requirements}


async def _read_ledger(*, session_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    raw = await _read_file(_LEDGER_PATH, session_id=session_id, user_id=user_id)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) and obj.get("requirements") else None
    except (json.JSONDecodeError, ValueError):
        return None


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
    worker_max_iters: int,
    enable_thinking: bool,
    chat_mode: Optional[str],
    emit: Optional[EmitFn],
    is_cancelled: Optional[CancelFn],
    project_ctx: Optional[Dict[str, Any]] = None,
    chat_id: Optional[str] = None,
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
        sandbox_session_id=session_id,  # key: same session → files persist across iterations
        project_ctx=project_ctx,        # key: bind to the user-selected project (site source workspace)
        chat_id=chat_id,
        isolated=True,  # independent MCP clients per iteration, avoids cross-task cancel-scope
        max_iters=worker_max_iters,
    )
    sa = StreamingAgent(agent, clients)
    text = ""
    tool_calls = 0
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
            },
        ):
            if is_cancelled and is_cancelled():
                break
            # Forward the worker's per-iteration streaming events in the **same SSE format as the
            # main conversation**, so the frontend renders them through the same pipeline as a normal
            # chat (content/tool_call/tool_result) rather than as an "iteration N" block.
            if et == "text_delta":
                text += payload
                if emit:
                    await emit({"type": "content", "event": "ai_message", "delta": payload})
            elif et == "thinking_delta":
                if emit:
                    await emit({"type": "thinking", "delta": payload})
            elif et == "tool_call":
                tool_calls += 1
                if emit:
                    await emit({"type": "tool_call", "tool_name": payload.get("name"),
                                "tool_id": payload.get("id"), "tool_args": payload.get("args")})
            elif et == "tool_result":
                if emit:
                    await emit({"type": "tool_result", "tool_name": payload.get("name"),
                                "tool_id": payload.get("id"), "result": payload.get("content")})
            elif et == "tool_pending":
                if emit:
                    await emit({"type": "tool_pending", **(payload or {})})
            elif et == "error":
                logger.warning("[loop] worker stream error: %s", payload)
    finally:
        usage = sa.get_usage()
        await close_clients(clients)
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
        ),
        workspace_note,
    ]
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
    project_ctx: Optional[Dict[str, Any]] = None,
    chat_id: Optional[str] = None,
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
    in_project = bool(project_ctx)
    t0 = time.monotonic()
    history: List[Dict[str, Any]] = []
    handoff = ""
    feedback = ""
    tokens_spent = 0
    seq = 0
    final_score: Optional[float] = None
    # Acceptance criteria: fed to the reviewer sub-agent to verify the real output item by item (extracted once before the run, stored in the ledger, reused on resume).
    criteria: List[str] = list(goal_spec.acceptance_criteria or [])

    async def _persist_ledger(led: Dict[str, Any]) -> None:
        """Flush the ledger: dual-write to the sandbox (working cache) + DB mirror (reliable source of truth for resume)."""
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
    sbx_ledger = await _read_ledger(session_id=session, user_id=user_id)
    ledger = _pick_fresher_ledger(db_ledger, sbx_ledger)
    if ledger is not None:
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
        reqs = await decompose_requirements(
            goal_spec=goal_spec, model_name=evaluator_model or "fast", user_id=user_id,
        )
        ledger = _new_ledger(goal_spec.objective, reqs)
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
        if seq >= budget.max_iters:
            return f"达到最大迭代数 {budget.max_iters}"
        if time.monotonic() - t0 >= budget.max_wall_clock_s:
            return f"达到最大墙钟 {budget.max_wall_clock_s}s"
        if tokens_spent >= budget.max_tokens:
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
        await _emit(emit, {"type": "iteration_started", "seq": seq,
                           "requirement_id": req["id"], "progress": _progress_frac(ledger)})
        logger.info("[loop %s] iter %d req=%s (%s)", loop_id, seq, req["id"], _progress_frac(ledger))

        # 1) Worker runs one iteration (fresh context, fed only the current requirement)
        strategy_change = int(req.get("attempts", 0)) >= _STRATEGY_CHANGE_AFTER
        prompt = _build_requirement_prompt(
            objective=goal_spec.objective, ledger=ledger, req=req, seq=seq,
            handoff=handoff, feedback=feedback, strategy_change=strategy_change,
            in_project=in_project,
        )
        work = await _run_worker_iteration(
            prompt=prompt, session_id=session, user_id=user_id, model_name=model_name,
            worker_max_iters=worker_max_iters, enable_thinking=enable_thinking,
            chat_mode=chat_mode, emit=emit, is_cancelled=is_cancelled,
            project_ctx=project_ctx, chat_id=chat_id,
        )
        tokens_spent += work["tokens"]
        req["attempts"] = int(req.get("attempts", 0)) + 1
        if is_cancelled and is_cancelled():
            status, reason = "cancelled", "外部取消"
            break

        # 2) Read-only reviewer sub-agent: independently opens the **real produced files** to verify this requirement (never trusts the worker's self-report).
        review = await review_requirement(
            objective=goal_spec.objective, requirement_desc=req["description"],
            acceptance_criteria=criteria, worker_summary=work["text"],
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

        # 3) Decide the flip (passes: false→true); only the driver may flip — "done" must also pass an independent second-pass re-check.
        passed = False
        if verdict == DONE:
            confirm = await review_requirement(
                objective=goal_spec.objective, requirement_desc=req["description"],
                acceptance_criteria=criteria, worker_summary=work["text"],
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
            # Stagnation exit: a single item exhausted its attempts without passing → HITL suspend / otherwise mark blocked and skip (prevents a single-item infinite loop).
            if int(req.get("attempts", 0)) >= _MAX_ATTEMPTS_PER_REQ:
                if hitl_enabled:
                    await _persist_ledger(ledger)
                    status = "awaiting_human"
                    reason = f"需求 {req['id']} 连续 {req['attempts']} 轮未通过评审，请人工介入"
                    await _emit(emit, {"type": "loop_awaiting_human", "seq": seq, "reason": reason})
                    break
                req["blocked"] = True
                await _emit(emit, {"type": "loop_stagnation", "seq": seq,
                                   "requirement_id": req["id"], "attempts": req["attempts"]})
                logger.info("[loop %s] req %s blocked after %d attempts",
                            loop_id, req["id"], req["attempts"])
        feedback = review.get("feedback", "")

        # 6) Handoff + persist ledger/progress (for resume). git diff takes precedence over the text summary.
        git_diff = await _git_diff_stat(session, user_id)
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
