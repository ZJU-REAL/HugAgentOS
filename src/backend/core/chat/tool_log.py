"""Tool-call log helpers for streaming SSE event processing.

Pure list/dict utilities that assemble the ``tool_calls_log`` consumed by both
the chat route (``api/routes/v1/chats/__init__.py``) and the background run executor
(``routing/chat_run_executor.py``). Relocated here from the chat route so
``routing.*`` no longer imports an API route module (breaks ``routing → api``).
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional
from core.chat.display_bounds import bound_result_for_display

logger = logging.getLogger(__name__)


def now_ms() -> int:
    """墙钟毫秒。工具卡的计时基准必须是 epoch 而非单调时钟——它要发给浏览器，
    还要落库供另一台设备读回来对齐，跨进程只有墙钟可比。"""
    return int(time.time() * 1000)


class SegmentRecorder:
    """Record the assistant message's display shape while it streams.

    The order of body text, reasoning and tool cards is already known the moment
    each piece is emitted, so it is simply written down here and persisted as
    ``metadata.segments``. Replay reads that list and renders it.

    Nothing reconstructs the order afterwards. Reconstructing it from a
    character offset into the body cannot work: the offset only moves when
    visible text is emitted, so everything a turn does between two sentences
    shares one value, and reasoning and tool cards live in separate columns —
    replay could only guess, and it guessed "all the reasoning, then all the
    cards" instead of the way they actually ran.

    Body text is stored inline (it is capped alongside ``content``); reasoning
    and tool cards are referenced by their index in ``thinking`` / ``tool_calls``
    so a long reasoning block is never stored twice.
    """

    def __init__(self) -> None:
        self._segments: List[Dict[str, Any]] = []
        self._pending_text = ""
        self._placed_tools = 0

    def reset(self) -> None:
        """Start a fresh assistant message (steer split, or a redrafted body)."""
        self._segments = []
        self._pending_text = ""
        self._placed_tools = 0

    def add_text(self, delta: str) -> None:
        if delta:
            self._pending_text += delta

    def _flush_text(self) -> None:
        if self._pending_text:
            self._segments.append({"type": "text", "text": self._pending_text})
            self._pending_text = ""

    def add_thinking(self, index: int) -> None:
        """Place the reasoning block just appended at ``thinking[index]``."""
        self._flush_text()
        self._segments.append({"type": "thinking", "index": index})

    def add_tools(self, tool_calls_log: List[Dict[str, Any]]) -> None:
        """Place every card that has appeared since the last call, in order."""
        if len(tool_calls_log) <= self._placed_tools:
            return
        self._flush_text()
        for index in range(self._placed_tools, len(tool_calls_log)):
            self._segments.append({"type": "tool", "index": index})
        self._placed_tools = len(tool_calls_log)

    def payload(self) -> Optional[List[Dict[str, Any]]]:
        self._flush_text()
        return self.snapshot()

    def snapshot(self) -> Optional[List[Dict[str, Any]]]:
        """Read-only view: the text run in progress stays open (mid-stream persistence)."""
        segments = list(self._segments)
        if self._pending_text:
            segments.append({"type": "text", "text": self._pending_text})
        return segments or None


def build_thinking_event(chunk: dict, chat_id: str) -> Dict[str, Any]:
    """Translate a ``thinking`` workflow chunk into its SSE event dict.

    Shared by the chat route and the background run executor so the
    thinking-event shape lives in one place. Sink-agnostic — the caller
    yields it as SSE or pushes it via ``_emit``.
    """
    evt: Dict[str, Any] = {"type": "thinking", "chat_id": chat_id}
    if chunk.get("structured_reasoning") is True:
        evt["structured_reasoning"] = True
    if "delta" in chunk:
        evt["delta"] = chunk.get("delta", "")
    else:
        evt["message"] = chunk.get("message", "正在思考...")
    return evt


def build_tool_call_start_event(chunk: dict, chat_id: str) -> Dict[str, Any]:
    """Build the transient event that opens one tool card by stable id.

    Start/delta events are transport state only. They intentionally do not
    mutate ``tool_calls_log``; the completed ``tool_call`` remains the single
    persisted source of truth.
    """
    evt: Dict[str, Any] = {
        "type": "tool_call_start",
        "tool_name": chunk.get("tool_name"),
        "tool_display_name": chunk.get("tool_display_name"),
        "tool_id": chunk.get("tool_id"),
        "chat_id": chat_id,
    }
    if chunk.get("scope"):
        evt["scope"] = chunk["scope"]
    return evt


def build_tool_call_delta_event(chunk: dict, chat_id: str) -> Dict[str, Any]:
    """Build one incremental JSON-argument fragment for an open tool card."""
    evt: Dict[str, Any] = {
        "type": "tool_call_delta",
        "tool_name": chunk.get("tool_name"),
        "tool_id": chunk.get("tool_id"),
        "arguments_delta": chunk.get("arguments_delta", ""),
        "chat_id": chat_id,
    }
    if chunk.get("scope"):
        evt["scope"] = chunk["scope"]
    return evt


def build_tool_call_event(chunk: dict, chat_id: str, tool_calls_log: list) -> Dict[str, Any]:
    """Build the ``tool_call`` SSE event and upsert it into ``tool_calls_log``.

    The log mutation is part of the shared semantics (both call sites upsert
    before emitting). Returns the event dict for the caller to sink.
    """
    tc: Dict[str, Any] = {
        "tool_name": chunk.get("tool_name"),
        "tool_display_name": chunk.get("tool_display_name"),
        "tool_args": chunk.get("tool_args", {}),
        "tool_id": chunk.get("tool_id"),
    }
    if chunk.get("subagent_name"):
        tc["subagent_name"] = chunk["subagent_name"]
    if chunk.get("scope"):
        tc["scope"] = chunk["scope"]
    upsert_tool_call(tool_calls_log, tc)
    return {"type": "tool_call", **tc, "chat_id": chat_id}


def build_user_question_event(chunk: dict, chat_id: str) -> Dict[str, Any]:
    """Build the authoritative pending-question request sent to the composer."""

    return {
        "type": "user_question",
        "chat_id": chat_id,
        "request_id": chunk.get("request_id"),
        "questions": chunk.get("questions", []),
        "created_at": chunk.get("created_at"),
        "expires_at": chunk.get("expires_at"),
    }


def build_user_question_resolved_event(chunk: dict, chat_id: str) -> Dict[str, Any]:
    """Build the server-owned event that removes a composer request."""

    return {
        "type": "user_question_resolved",
        "chat_id": chat_id,
        "request_id": chunk.get("request_id"),
        "outcome": chunk.get("outcome"),
    }


def _payload_carries_error(res: Any) -> bool:
    """结果载荷顶层带了非空 ``error`` 字段 → 这次调用是失败的。

    只认**顶层**的 error，且只认 dict / 能解析成 dict 的 JSON 串：往结果里嵌了 error
    字样的正常内容（比如搜到一篇讲错误码的网页）不该被误判成故障。

    还要穿透一层 **MCP 内容块**（``[{"type": "text", "text": "{...}"}]``）——MCP 工具的
    返回就是这个形状，不穿透的话判定永远落空：实测搜索有一半调用被上游 429 打回，审计面
    却依然显示"全部成功"。穿透只做一层、且每块仍走上面那套严格判定，不会放宽误判风险。
    """
    payload = res
    if isinstance(payload, (list, tuple)):
        return any(
            _payload_carries_error(blk.get("text"))
            for blk in payload
            if isinstance(blk, dict) and isinstance(blk.get("text"), str)
        )
    if isinstance(payload, str):
        text = payload.strip()
        if not text.startswith("{"):
            return False
        try:
            payload = json.loads(text)
        except Exception:  # noqa: BLE001
            return False
    return isinstance(payload, dict) and bool(payload.get("error"))


def build_tool_result_event(chunk: dict, chat_id: str, tool_calls_log: list) -> Dict[str, Any]:
    """Build the ``tool_result`` SSE event and attach it into ``tool_calls_log``.

    Attach-vs-emit ordering is irrelevant: the log is only read later at
    persist time, and the returned event is fully built before the attach.
    """
    tid = chunk.get("tool_id")
    tn = chunk.get("tool_name")
    res = chunk.get("result", {})
    raw_status = str(chunk.get("status") or "success").lower()
    # A steer interrupts the pending tool *before* execution so the model can
    # re-plan with the user's new instruction.  It is a normal control-flow
    # boundary, not a failed tool call: preserve the distinct status for audit
    # and let the UI render it neutrally instead of showing a red error cross.
    if raw_status == "interrupted":
        status = "interrupted"
    elif raw_status in {"error", "denied"}:
        status = "error"
    elif _payload_carries_error(res):
        # 工具把上游故障"温柔地"包成 {"error": ...} 正常返回（MCP 里很常见），
        # 于是审计面记成 success。实测后果：搜索有 36% 的调用被上游 429 打回，
        # 统计上却是 227 成功 0 失败——排障时第一眼就被带偏。载荷里写着 error 就是 error。
        status = "error"
    else:
        status = "success"
    # 推给浏览器的是**裁剪过的展示副本**；下面 attach_tool_result 落库的仍是完整的
    # res（审计与事后回查要用）。读一个 5MB 文件时，这两份的差别就是标签页活不活。
    display_res, _clipped = bound_result_for_display(res)
    evt: Dict[str, Any] = {
        "type": "tool_result",
        "tool_name": tn,
        "result": display_res,
        "tool_id": tid,
        "chat_id": chat_id,
        "citations": chunk.get("citations", []),
        "status": status,
    }
    if chunk.get("subagent_name"):
        evt["subagent_name"] = chunk["subagent_name"]
    if chunk.get("scope"):
        evt["scope"] = chunk["scope"]
    duration_ms = attach_tool_result(tool_calls_log, tid, tn, res, status=status)
    if duration_ms is not None:
        evt["duration_ms"] = duration_ms
    return evt


def upsert_tool_call(tool_calls_log: list, tc: dict) -> None:
    """Merge a tool_call into the log, updating an existing entry by tool_id.

    A newly opened entry is stamped with its start time here — every path that
    builds a ``tool_calls_log`` (chat, plan, automation, batch) goes through
    this function, so the stamp is an invariant of the log rather than
    something each caller has to remember. A repeat event for a call already
    open backfills arguments and leaves the original stamp alone.

    Only an entry that is still **open** absorbs the event. One log spans every
    ReAct round of one assistant message, while a gateway that numbers its ids
    per response hands out ``call_0`` again next round — merging into the closed
    entry would have filed the second call's arguments and result on top of the
    first one's, losing a whole call. Ids are made unique within a response one
    layer up, in ``core/llm/tool_call_identity.py``; across responses the log is
    where it has to hold.
    """
    tid = tc.get("tool_id")
    if tid:
        for existing in tool_calls_log:
            if existing.get("tool_id") == tid and "result" not in existing:
                if tc.get("tool_args"):
                    existing["tool_args"] = tc["tool_args"]
                if tc.get("tool_display_name"):
                    existing["tool_display_name"] = tc["tool_display_name"]
                return
    tc.setdefault("started_at", now_ms())
    tool_calls_log.append(tc)


def _settle_tool_call(tc: dict, res: Any, status: str) -> Optional[int]:
    """Close one entry: attach the result and freeze how long the call took.

    ``duration_ms`` is stored alongside the result because the log is what a
    reload — or another device — reads back. Without it the card can only time
    itself from the moment it was rendered, so a replayed run restarts every
    clock at zero.
    """
    tc["result"], tc["status"] = res, status
    started_at = tc.get("started_at")
    if not isinstance(started_at, int):
        return None
    tc["duration_ms"] = max(0, now_ms() - started_at)
    return tc["duration_ms"]


def attach_tool_result(
    tool_calls_log: list,
    tid: str,
    tn: str,
    res: Any,
    *,
    status: str = "success",
) -> Optional[int]:
    """Attach a tool_result to the matching tool_call entry in the log.

    Returns the call's duration in ms when the opening entry carried a start
    stamp, so the caller can put the same number on the wire.
    """
    # Identity is the call id and nothing else. Matching on the tool name as a
    # fallback used to cross-attach parallel calls: with eight read_image calls
    # open, every result matched entry #1 by name before reaching its own entry,
    # so args and results ended up describing different files and duration_ms
    # was measured against another call's start stamp. The name fallback also
    # had nothing left to do — every call carries an id from AgentScope's
    # required ToolCallBlock.id (62,926 persisted calls checked, none missing).
    #
    # Among entries sharing an id, the open one is the call being closed: a
    # gateway that restarts its numbering every round lets ``call_0`` name both
    # an earlier finished call and the one running now.
    if tid:
        for tc in tool_calls_log:
            if tc.get("tool_id") == tid and "result" not in tc:
                return _settle_tool_call(tc, res, status)
        for tc in tool_calls_log:
            if tc.get("tool_id") == tid:
                # Every entry with this id is already closed — a repeat delivery
                # of a result that was recorded once.
                return _settle_tool_call(tc, res, status)
    if tid or tn:
        # No opening entry to close (a result that arrived without its call):
        # record it, but leave duration unknown rather than inventing one.
        # ``call_missing`` is what tells a reader apart from a normal card: this
        # one has a result and no arguments because its call never reached the
        # log, not because the tool takes none. Without the flag the anomaly is
        # indistinguishable from a healthy zero-argument call.
        # A call the user steered away from is aborted before it ever reaches the
        # wire, so having no entry to close is its normal shape — the flag still
        # goes on the row, but it is not something an operator has to look at.
        logger.log(
            logging.INFO if status == "interrupted" else logging.WARNING,
            "tool result %s (%s) has no call entry to close; recorded on its own",
            tid or "<no id>",
            tn or "<no name>",
        )
        tool_calls_log.append(
            {
                "tool_name": tn,
                "tool_id": tid,
                "result": res,
                "status": status,
                "call_missing": True,
            }
        )
    return None


# Persistence caps for sub-agent sub-steps (prevent a single call_subagent's sub_steps from growing unbounded and bloating the message row).
_SUBSTEP_OUTPUT_CAP = 16000  # max characters stored per sub-tool result
_SUBSTEP_MAX_STEPS = 200  # max sub-steps stored per call_subagent card


def _upsert_tool_step(steps: list, tid: Any, name: str, patch: dict) -> None:
    """Merge a sub-tool step by toolId: on hit, merge patch (+name); otherwise append (subject to the step-count cap).

    A step is only merged when it has an id to be merged *by*. Without the
    guard, ``toolId == tid`` also holds for two id-less steps, so two sub-tools
    a sub-agent ran in parallel collapsed into a single step whose arguments and
    output came from different calls. The frontend counterpart in
    ``chatStream.ts::applySubagentEvent`` has always had this guard.
    """
    if tid:
        for s in steps:
            if s.get("kind") == "tool" and s.get("toolId") == tid:
                s.update(patch)
                if name:
                    s["name"] = name
                return
    if len(steps) < _SUBSTEP_MAX_STEPS:
        steps.append(
            {"kind": "tool", "toolId": tid, "name": name or "tool", "status": "running", **patch}
        )


def attach_subagent_step(tool_calls_log: list, parent_tool_id: str, ev: dict) -> None:
    """Accumulate one subagent_event into the ``sub_steps`` of the matching
    call_subagent entry, so the sub-agent's internal process can be replayed
    after a refresh.

    Merge rules mirror the frontend's applySubagentEvent: tool_call is merged
    by tool_id (start has no args → end backfills them), tool_result backfills
    output/status, thinking is merged incrementally. Does **not** persist
    ``content`` (i.e. the sub-agent's answer itself, already in the
    call_subagent tool_result) nor start/end/error control events. Capped to
    prevent bloat.
    """
    if not parent_tool_id:
        return
    # 批量作业的中途进度只活在实时流里：它贴的是 run_job 的工具卡，落库只会留下一行
    # 过期数字（刷新后 tool_result 才是结论），还会把 agent_name 写成「批量作业」污染
    # 那张卡的展示名。
    if ev.get("sub_type") == "job_progress":
        return
    # 子步骤是父调用**还在跑的时候**冒出来的，所以同 id 的条目里认未收口的那条
    # （同 upsert_tool_call：跨轮复用 id 的网关会让一个 id 同时指向上一轮已结束的
    # 调用和这一轮正在跑的调用）。
    candidates = [tc for tc in tool_calls_log if tc.get("tool_id") == parent_tool_id]
    entry = next((tc for tc in candidates if "result" not in tc), None) or next(
        iter(candidates), None
    )
    if entry is None:
        return

    if ev.get("agent_name") and not entry.get("subagent_name"):
        entry["subagent_name"] = ev["agent_name"]

    steps = entry.setdefault("sub_steps", [])
    st = ev.get("sub_type")

    if st == "tool_call":
        inp = ev.get("input")
        _upsert_tool_step(
            steps, ev.get("tool_id"), ev.get("tool_name"), {"input": inp} if inp is not None else {}
        )

    elif st == "tool_result":
        out = ev.get("output")
        if isinstance(out, str) and len(out) > _SUBSTEP_OUTPUT_CAP:
            out = out[:_SUBSTEP_OUTPUT_CAP] + "…（已截断）"
        status = "error" if ev.get("status") == "error" else "success"
        _upsert_tool_step(
            steps, ev.get("tool_id"), ev.get("tool_name"), {"output": out, "status": status}
        )

    elif st == "thinking":
        delta = ev.get("delta") or ""
        if not delta:
            return
        if steps and steps[-1].get("kind") == "thinking":
            cur = steps[-1].get("text") or ""
            if len(cur) < _SUBSTEP_OUTPUT_CAP:
                steps[-1]["text"] = cur + delta
        elif len(steps) < _SUBSTEP_MAX_STEPS:
            steps.append({"kind": "thinking", "text": delta})
