"""Loop reviewer subagent — the autonomous loop's "output reviewer" (replaces script verification + self-reported text judgment).

Design motivation (see the lesson from trace 435be138): without a verify command, the old
evaluator degraded into an LLM reading the **worker's self-reported text summary** to
judge completion — if the worker said "I'm done" the evaluator believed it, scoring 5/5
even when the site hadn't actually changed. This module hands "judgment" to an
**independent, tool-equipped, read-only** subagent: it binds to the **same project
sandbox session** as the worker and personally opens the real produced files with
read/grep/glob/bash to verify, instead of trusting any self-reported text.

Key constraints:
  1. **Independent**: the reviewer and the evaluated worker are two agents with two
     contexts; the reviewer does not reuse the worker's conversation, receiving only
     "objective + current requirement + acceptance criteria", and gathers evidence from
     the environment itself.
  2. **Read-only**: the reviewer is a judge, not a player — the system prompt explicitly
     forbids modifying any file, allowing only reading/searching/running read-only
     commands. Whether it could write doesn't matter (the driver wouldn't accept its
     writes anyway); semantically it only verifies.
  3. **Look at real output**: the verdict must cite file contents / command output it
     **personally read** (the evidence field); a done with empty evidence, or one guessed
     purely from the requirement description, is always downgraded to continue.

Returns ``{verdict, criteria_hit, evidence, feedback}``, with verdict semantics aligned
to the old evaluate_iteration (done/continue/off_track/need_human), so the driver can
route on it directly.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.infra.logging import get_logger
from orchestration.loop_evaluator import (
    CONTINUE,
    DONE,
    NEED_HUMAN,
    OFF_TRACK,
    _parse_json_lenient,
)

logger = get_logger(__name__)

EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]

_VALID_VERDICTS = (DONE, CONTINUE, OFF_TRACK, NEED_HUMAN)
# The reviewer itself needs to run a few tool steps (ls/read/grep) to gather evidence; give it a small but sufficient step cap.
_REVIEWER_MAX_ITERS = 12


def _build_review_prompt(
    *,
    objective: str,
    requirement_desc: str,
    acceptance_criteria: List[str],
    worker_summary: str,
    second_pass: bool,
) -> str:
    criteria = "\n".join(f"- {c}" for c in acceptance_criteria) or "- （无显式验收标准，按需求描述核验）"
    parts = [
        "你是一个自主循环的**独立产出评审员**。你不是执行者，是裁判。",
        "你和刚才干活的执行 agent 是**两个人**——你**绝不能**采信它自报的"
        "「我已完成 / 我加上了 / 我优化了」之类的话。你的唯一职责是：**亲自打开当前"
        "项目里产出的真实文件**（用 ls / view_text_file / grep / glob / bash 等只读手段），"
        "核对下面这条需求到底有没有真正落地。",
        "\n## ⛔ 只读约束\n你**禁止**创建、修改、删除任何文件，也不要发布/构建改动。"
        "只允许读取、检索、运行**只读**命令来取证。",
        f"\n## 总目标\n{objective}",
        f"\n## 本轮要核验的需求\n{requirement_desc}",
        f"\n## 验收标准（逐条核对）\n{criteria}",
    ]
    if worker_summary.strip():
        parts.append(
            "\n## 执行 agent 的自述（**仅作线索，不是证据**）\n"
            f"{worker_summary.strip()[:1200]}\n"
            "⚠️ 上面是它自己说的，可能夸大或与实际文件不符。你必须去文件里亲自验证。"
        )
    parts.append(
        "\n## 取证步骤\n"
        "1. 先 `ls` / glob 摸清项目里有哪些相关文件（HTML/JS/CSS/组件/数据等）。\n"
        "2. 打开与本需求直接相关的文件，读它的真实内容。\n"
        "3. 对照验收标准逐条判断：内容里是否**确实**出现了需求要求的东西"
        "（如某个功能模块、某段文案、某种交互/布局），还是只是被声称做了。\n"
        "4. 如需求涉及「能跑/能构建/能通过」，用只读命令实际验证（如 grep 关键实现、"
        "查语法、必要时构建到临时目录）。"
    )
    if second_pass:
        parts.append(
            "\n## ⚠️ 二次复核\n这是对「已判定完成」的**独立复核**。请以更严格的标准重新取证，"
            "只要有一条验收标准无法从真实文件里找到确凿证据，就判 continue。"
        )
    parts.append(
        "\n## 输出（严格 JSON，不要多余文字）\n"
        '{"verdict": "done|continue|off_track|need_human", '
        '"criteria_hit": ["已确凿满足的验收标准原文", ...], '
        '"evidence": "你**亲自读到**的文件路径 + 关键内容片段/命令输出，作为判定依据", '
        '"feedback": "若未完成：具体还差什么、下一轮该改哪个文件的什么；若完成：一句话结论"}\n'
        "判定纪律：**只有当每一条验收标准都能被你引用到的真实文件证据支撑时**才输出 done；"
        "证据不足、找不到对应产出、或只有自报没有实物，一律 continue（绝不放水）。"
    )
    return "\n".join(parts)


async def review_requirement(
    *,
    objective: str,
    requirement_desc: str,
    acceptance_criteria: List[str],
    worker_summary: str,
    session_id: str,
    user_id: str,
    project_ctx: Optional[Dict[str, Any]] = None,
    chat_id: Optional[str] = None,
    model_name: Optional[str] = None,
    second_pass: bool = False,
    requirement_id: Optional[str] = None,
    emit: Optional[EmitFn] = None,
) -> Dict[str, Any]:
    """Spawn a **read-only** review sub-agent that personally verifies whether the current requirement is truly delivered.

    This is a **first-class platform sub-agent** (on par with call_subagent / plan_mode):
      - Deterministically triggered by the driver (not the worker) — keeping maker≠checker (the Codex/Claude Code goal-mode consensus);
      - ``read_only=True`` registers no file-modifying tools (on par with Codex reviewer's sandbox_mode=read-only);
      - Bound to the **same** ``session_id`` + ``project_ctx`` as the worker — so it reads exactly the project files the
        worker actually changed (where the site source lives);
      - Writes ``subagent_call_logs`` (type=loop_reviewer) + ``subagent_scope`` so its internal tool calls are attributed to this
        review record, auditable under "Config console → Sub-agent call logs";
      - ``emit`` optionally sends ``loop_review_started`` / ``loop_review_result`` events to the loop SSE stream (observability).

    Returns ``{verdict, criteria_hit, evidence, feedback}``; on any exception/parse failure conservatively continue (never misjudge as done).
    """
    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients
    from core.services import log_service as log_writer
    from orchestration.streaming import StreamingAgent

    phase = "复核" if second_pass else "核验"
    if emit:
        try:
            await emit({"type": "loop_review_started", "requirement_id": requirement_id,
                        "second_pass": second_pass})
        except Exception:  # noqa: BLE001
            pass

    # First-class sub-agent call log (best-effort, never blocks the review).
    _t0 = time.monotonic()
    sub_log_id = await log_writer.start_subagent_log({
        "subagent_name": f"循环评审员（{phase}）",
        "subagent_type": "loop_reviewer",
        "user_id": user_id,
        "chat_id": chat_id,
        "model": model_name,
        "input_messages": {"requirement_id": requirement_id, "requirement": requirement_desc,
                           "acceptance_criteria": acceptance_criteria, "second_pass": second_pass},
    })
    tool_calls = 0

    async def _finish(status: str, *, output: str = "", error: Optional[str] = None) -> None:
        try:
            await log_writer.finish_subagent_log(
                sub_log_id, status=status, output_content=output or None,
                tool_calls_count=tool_calls, error_message=error,
                duration_ms=int((time.monotonic() - _t0) * 1000),
            )
        except Exception:  # noqa: BLE001
            pass

    prompt = _build_review_prompt(
        objective=objective,
        requirement_desc=requirement_desc,
        acceptance_criteria=acceptance_criteria,
        worker_summary=worker_summary,
        second_pass=second_pass,
    )
    try:
        agent, clients = await create_agent_executor(
            current_user_id=user_id,
            model_name=model_name,
            sandbox_session_id=session_id,   # key: same sandbox as the worker → reads real output
            project_ctx=project_ctx,          # key: scope to the project folder (where site source lives)
            chat_id=chat_id,
            enabled_skill_ids=[],             # pure verification, load no business skills
            isolated=True,                    # independent MCP client, avoid cross-task cancel-scope
            max_iters=_REVIEWER_MAX_ITERS,
            read_only=True,                   # read-only: register no file-modifying tools (the judge isn't a player)
        )
    except Exception as exc:  # noqa: BLE001 - a reviewer agent that won't start must not drag down the loop
        logger.warning("[loop-review] spawn reviewer failed: %s", exc)
        await _finish("failed", error=str(exc)[:200])
        return {"verdict": CONTINUE, "criteria_hit": [], "evidence": "",
                "feedback": "评审子智能体启动失败，保守继续。"}

    sa = StreamingAgent(agent, clients)
    text = ""
    try:
        # The reviewer's stream is **not forwarded** to the user bubble — it's an internal judge, only its final text verdict is collected.
        # subagent_scope attributes the reviewer's internal read/grep/... tool calls to this sub-agent log (auditable).
        with log_writer.subagent_scope(sub_log_id, source="loop_reviewer"):
            async for et, payload in sa.stream(
                [{"role": "user", "content": prompt}],
                {"user_id": user_id, "model_name": model_name or "",
                 "enable_thinking": False, "chat_mode": "medium"},
            ):
                if et == "text_delta":
                    text += payload
                elif et == "tool_call":
                    tool_calls += 1
                elif et == "error":
                    logger.warning("[loop-review] reviewer stream error: %s", payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[loop-review] reviewer run failed: %s", exc)
    finally:
        await close_clients(clients)

    async def _return(result: Dict[str, Any], *, log_status: str = "success") -> Dict[str, Any]:
        await _finish(log_status, output=(result.get("verdict", "") + " | " + result.get("feedback", ""))[:2000])
        if emit:
            try:
                await emit({"type": "loop_review_result", "requirement_id": requirement_id,
                            "second_pass": second_pass, "verdict": result.get("verdict"),
                            "evidence": (result.get("evidence") or "")[:600]})
            except Exception:  # noqa: BLE001
                pass
        return result

    obj = _parse_json_lenient(text)
    if not isinstance(obj, dict) or obj.get("verdict") not in _VALID_VERDICTS:
        # Can't parse a structured verdict → conservatively continue (never misjudge as done).
        logger.info("[loop-review] unparseable verdict, defaulting continue")
        return await _return({"verdict": CONTINUE, "criteria_hit": [], "evidence": text[:400],
                              "feedback": "评审未给出可解析的结论，保守继续。"})
    # evidence fallback: done but no evidence given → downgrade to continue (prevent empty-evidence leniency).
    evidence = str(obj.get("evidence", "") or "").strip()
    if obj["verdict"] == DONE and not evidence:
        return await _return({"verdict": CONTINUE, "criteria_hit": obj.get("criteria_hit", []),
                              "evidence": "", "feedback": "评审判定完成但未给出文件证据，视为未确证，继续。"})
    return await _return({
        "verdict": obj["verdict"],
        "criteria_hit": obj.get("criteria_hit", []) or [],
        "evidence": evidence,
        "feedback": str(obj.get("feedback", "") or "").strip(),
    })
