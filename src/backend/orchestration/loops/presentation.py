"""Build persisted Loop transcripts and map terminal statuses."""

from __future__ import annotations

from core.infra.logging import get_logger

logger = get_logger(__name__)

_LOOP_STATUS_ZH = {
    "completed": "✅ 已达成",
    "budget_exhausted": "⏳ 预算耗尽",
    "cancelled": "⛔ 已取消",
    "awaiting_human": "🙋 等待人工",
    "failed": "❌ 失败",
}


def _loop_transcript_md(objective: str, result) -> str:
    """Render one autonomous loop's iteration trace into an assistant message for the chat (markdown)."""
    lines = [
        "**🔁 自主循环**",
        "",
        f"**目标**：{objective}",
        "",
        "| 轮次 | 需求 | 评审判定 | 工具 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in result.history:
        reason = str(r.get("reason") or "").replace("\n", " ").replace("|", "／")
        if len(reason) > 60:
            reason = reason[:60] + "…"
        lines.append(
            f"| {r.get('seq')} | {r.get('requirement_id', '')} | {r.get('verdict')} | "
            f"{r.get('tool_calls', 0)} | {reason} |"
        )
    status_zh = _LOOP_STATUS_ZH.get(result.status, result.status)
    # final_score is now the requirement pass ratio (0~1) — no script-based numeric score.
    final = "—" if result.final_score is None else f"{result.final_score:.0%}"
    lines += [
        "",
        f"**结果**：{status_zh} · 共 {result.iterations} 轮 · 完成度 {final}"
        f" · 用时 {result.wall_clock_s:.0f}s",
    ]
    if result.reason:
        lines.append(f"\n> {result.reason}")
    return "\n".join(lines)


def record_loop_event(event: dict, content_buf: list[str], tool_log: list[dict], tool_idx: dict[str, int]) -> None:
    """Accumulate only visible answer text and tool cards for the loop transcript."""
    et = event.get("type")
    if et == "content":
        _delta = event.get("delta")
        if _delta:
            content_buf.append(str(_delta))
    elif et == "tool_call":
        _tid = str(event.get("tool_id") or f"t{len(tool_log)}")
        tool_idx[_tid] = len(tool_log)
        tool_log.append(
            {
                "id": _tid,
                "name": event.get("tool_name") or "tool",
                "input": event.get("tool_args"),
                "status": "running",
            }
        )
    elif et == "tool_result":
        _i = tool_idx.get(str(event.get("tool_id") or ""))
        if _i is not None:
            tool_log[_i]["output"] = event.get("result")
            tool_log[_i]["status"] = "error" if event.get("error") else "success"
