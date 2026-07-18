"""Tool-call soft warnings (observation only — never blocks).

Historical background: after the AgentScope migration, tool progress goes through
routing/streaming.py's msg_queue polling, and the old callback mechanism was deprecated.
This module is revived as **observation only**: once the code-execution capability is
enabled in all modes (CODE_CAPABILITY_ENABLED), the default tool set grows larger, and the
model may waver between semantically overlapping tools (docs/code-execution-merge-proposal
§3.4 / §5.5). Here, during the gradual rollout, we record suspicious co-occurrences to help
diagnose whether the model goes off track. **Does not block any call**, and never raises.

Usage (called once per tool_call by the streaming layer):

    from orchestration.tool_callbacks import note_tool_call
    note_tool_call(state, tool_name, tool_args)

``state`` is an arbitrary per-run mutable dict (held by StreamingAgent); this module manages its own keys.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Office MCP prefixes: semantically overlap with bash's do-anything capability (two paths to the same thing).
_OFFICE_PREFIXES = ("word_", "excel_", "ppt_", "pdf_")
# /myspace write-class tools: after the merge, wrongly modifying the user's private drive is the
# highest risk (§13 is the hard safeguard; here we only observe who writes /myspace during rollout).
_MYSPACE_WRITE_TOOLS = {"Write", "Edit", "Delete", "Move"}


def _arg_paths(tool_args: Any) -> list[str]:
    """Best-effort extraction of path-like strings from tool_args (returns empty on failure)."""
    if not isinstance(tool_args, dict):
        return []
    out: list[str] = []
    for k in ("path", "file_path", "src_path", "dst_path", "dest_path"):
        v = tool_args.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    return out


def note_tool_call(
    state: Dict[str, Any],
    tool_name: Optional[str],
    tool_args: Any = None,
) -> None:
    """Record a tool call and log a warning for suspicious co-occurrences / sensitive writes.

    Each (run, conflict type) warns only once, to avoid flooding. Any exception is
    swallowed — observation code must never affect the main flow.
    """
    try:
        if not tool_name:
            return
        seen: set = state.setdefault("_tc_seen", set())
        warned: set = state.setdefault("_tc_warned", set())
        seen.add(tool_name)

        has_bash = "bash" in seen
        has_office = any(n.startswith(_OFFICE_PREFIXES) for n in seen)
        if has_bash and has_office and "bash_vs_office" not in warned:
            warned.add("bash_vs_office")
            logger.warning(
                "[tool-soft-warn] bash 与 Office MCP 同一 run 共现 "
                "(seen=%s) —— 可能语义摇摆，灰度观测",
                sorted(seen),
            )

        if "Read" in seen and "view_text_file" in seen \
                and "read_vs_view" not in warned:
            warned.add("read_vs_view")
            logger.warning(
                "[tool-soft-warn] Read 与 view_text_file 同一 run 共现 "
                "—— 读文件路径可能混淆，灰度观测",
            )

        if tool_name in _MYSPACE_WRITE_TOOLS:
            for p in _arg_paths(tool_args):
                if p.startswith("/myspace") or "/myspace/" in p:
                    logger.warning(
                        "[tool-soft-warn] %s 写 /myspace 路径 %s "
                        "—— 合并后最高风险点（§13 硬保险负责拦截），灰度观测",
                        tool_name, p,
                    )
                    break
    except Exception:  # noqa: BLE001 — observation must never affect the main flow
        logger.debug("[tool-soft-warn] note_tool_call failed", exc_info=True)
