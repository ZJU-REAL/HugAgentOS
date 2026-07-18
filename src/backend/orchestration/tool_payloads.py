"""SSE tool-result payload builders.

Pure presentation helpers that turn raw tool outputs into the compact
``file_preview`` / ``skill_detail`` payloads the frontend renders as tool cards.
Extracted from ``routing/workflow.py`` to slim that orchestrator down — these
have no orchestration logic, only formatting.
"""

from __future__ import annotations

from typing import Any, Dict

from core.config.catalog_loader import get_skill_curated_detail

_FILE_PREVIEW_MAX_LINES = 8
_FILE_PREVIEW_MAX_CHARS = 400


def _truncate_preview(text: str) -> str:
    """Truncate to ≤ 8 lines and ≤ 400 chars for compact tool-card display."""
    if not isinstance(text, str) or not text:
        return ""
    lines = text.splitlines()
    if len(lines) > _FILE_PREVIEW_MAX_LINES:
        lines = lines[:_FILE_PREVIEW_MAX_LINES] + ["…"]
    snippet = "\n".join(lines)
    if len(snippet) > _FILE_PREVIEW_MAX_CHARS:
        snippet = snippet[:_FILE_PREVIEW_MAX_CHARS].rstrip() + "…"
    return snippet


def _build_view_text_file_payload(tool_args: Any, tool_content: Any) -> Dict[str, Any]:
    """Build the view_text_file SSE payload: send only file metadata + a short preview, avoiding pushing the whole file to the frontend."""
    import os

    args = tool_args if isinstance(tool_args, dict) else {}
    file_path = str(args.get("file_path", "") or "")
    name = os.path.basename(file_path.replace("\\", "/")) if file_path else "文件"

    text = tool_content if isinstance(tool_content, str) else str(tool_content or "")
    total_chars = len(text)
    line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)

    extra: Dict[str, Any] = {}
    ranges = args.get("ranges")
    if isinstance(ranges, list) and ranges:
        extra["ranges"] = ranges

    return {
        "kind": "file_preview",
        "name": name,
        "path": file_path or None,
        "mime_type": None,
        "total_chars": total_chars,
        "returned_chars": total_chars,
        "has_more": False,
        "line_count": line_count,
        "extra": extra,
        "preview": _truncate_preview(text),
    }


def _build_read_tool_payload(tool_args: Any, result: Any) -> Dict[str, Any]:
    """Trim the result of the PascalCase `Read` tool (core/llm/tools/read_tool.py).

    Read returns several types:
      - text       → {file_path, content, start_line, end_line, total_lines, truncated, ...}
      - binary     → {file_path, size, hint}
      - too_large  → {file_path, size, hint}
      - error      → {error: '...'}
    All mapped uniformly to file_preview: content replaced with a ≤8-line preview, the rest of the metadata shown as chips.
    """
    import os

    args = tool_args if isinstance(tool_args, dict) else {}
    if not isinstance(result, dict):
        return {
            "kind": "file_preview",
            "name": os.path.basename(str(args.get("file_path", "")).replace("\\", "/")) or "文件",
            "path": args.get("file_path"),
            "preview": "",
            "extra": {},
        }

    if "error" in result:
        path = result.get("file_path") or args.get("file_path") or ""
        return {
            "kind": "file_preview",
            "name": os.path.basename(str(path).replace("\\", "/")) or "文件",
            "path": path or None,
            "preview": "",
            "extra": {"error": result.get("error")},
        }

    rtype = result.get("type", "text")
    path = result.get("file_path") or args.get("file_path") or ""
    name = os.path.basename(str(path).replace("\\", "/")) or "文件"

    if rtype in ("binary", "too_large"):
        size = result.get("size")
        extra: Dict[str, Any] = {"type": rtype}
        if isinstance(size, int):
            extra["size_bytes"] = size
        if result.get("hint"):
            extra["hint"] = result["hint"]
        return {
            "kind": "file_preview",
            "name": name,
            "path": path or None,
            "preview": "",
            "extra": extra,
        }

    # type == "text"
    content = result.get("content") if isinstance(result.get("content"), str) else ""
    extra2: Dict[str, Any] = {}
    for k in ("start_line", "end_line", "truncated", "parsed_text",
              "recovered_from_artifact", "persistent", "physical_path"):
        if k in result and result[k] not in (None, "", False):
            extra2[k] = result[k]
    return {
        "kind": "file_preview",
        "name": name,
        "path": path or None,
        "mime_type": None,
        "total_chars": None,
        "returned_chars": None,
        "has_more": bool(result.get("truncated")),
        "line_count": result.get("total_lines"),
        "extra": extra2,
        "preview": _truncate_preview(content),
    }


def _build_read_artifact_payload(result: Any) -> Dict[str, Any]:
    """Trim the read_artifact result: drop the full content text, keep only metadata + a short preview."""
    if not isinstance(result, dict):
        return {
            "kind": "file_preview",
            "name": "artifact",
            "path": None,
            "mime_type": None,
            "preview": "",
            "extra": {},
        }
    # output structure from the read_artifact tool (see core/llm/tool.py::read_artifact)
    content = result.get("content")
    extra: Dict[str, Any] = {}
    for k in ("file_id", "offset", "next_offset", "read_chars_so_far",
              "budget_remaining", "sheet_names", "slide_count", "slide_index"):
        if k in result and result[k] not in (None, ""):
            extra[k] = result[k]
    return {
        "kind": "file_preview",
        "name": result.get("filename") or result.get("file_id") or "artifact",
        "path": None,
        "mime_type": result.get("mime_type"),
        "total_chars": result.get("total_chars"),
        "returned_chars": result.get("returned_chars"),
        "has_more": bool(result.get("has_more")),
        "line_count": None,
        "extra": extra,
        "preview": _truncate_preview(content if isinstance(content, str) else ""),
    }


def _build_skill_load_payload(skill_id: str) -> Dict[str, Any]:
    """Build the SSE payload for the load_skill tool result: lightweight structured data, same shape as the capability center.

    Returns something like {"kind": "skill_detail", "skill_id", "name", "description", "version", "tags", "detail"}.
    detail is the user_intro markdown; when skill_id is not found, only placeholder fields are filled and the frontend falls back to a default display.
    """
    sid = (skill_id or "").strip()
    spec = get_skill_curated_detail(sid) if sid else None
    if not spec:
        return {
            "kind": "skill_detail",
            "skill_id": sid,
            "name": sid or "技能",
            "description": "",
            "version": "",
            "tags": [],
            "detail": "",
        }
    return {
        "kind": "skill_detail",
        "skill_id": spec.get("id", sid),
        "name": spec.get("name", sid),
        "description": spec.get("description", ""),
        "version": spec.get("version", ""),
        "tags": list(spec.get("tags", []) or []),
        "detail": spec.get("detail", ""),
    }


# Tools whose "running" card should appear immediately even when the first
# streaming chunk still has empty args. Needed for tools whose args take the
# LLM a noticeable amount of time to finish writing (e.g. long JSON params),
# otherwise the UI shows only the generic typing placeholder until the tool
# result finally arrives.
_FAST_EMIT_TOOLS = frozenset({
    "bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
})


def _tool_args_ready(tool_name: str, tool_args: Any) -> bool:
    """Check if tool_call args are complete enough to emit to the frontend.

    For view_text_file we need file_path to decide if it's a skill load.
    For tools in _FAST_EMIT_TOOLS we emit immediately (even with empty args)
    so the UI shows the card right away, then update when args arrive.
    """
    if not tool_args:
        if tool_name in _FAST_EMIT_TOOLS:
            return True
        return False
    if tool_name == "view_text_file":
        return bool(isinstance(tool_args, dict) and tool_args.get("file_path"))
    return True
