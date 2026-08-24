"""Pure-logic helpers: file-context building, per-turn state, model resolution.

AgentScope 2.0 replaced the old hook factories with the Middleware classes in
``core.llm.middlewares``; this module has been reduced to the pure-function
helpers those middlewares reuse, independent of the agentscope version
(``_build_file_context`` / ``_build_historical_files_context`` /
``_get_main_model`` / ``_resolve_chat_mode`` / pin-hint & read-budget state, etc.).
The runtime context moved from 1.x's ``agent._jx_context`` to ``agent.state``
(AgentRuntimeState).
"""

from __future__ import annotations

import base64
import logging
import re
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_FILE_CONTENT_CHARS = 50_000  # per-turn read_artifact budget per file

# Per-turn cumulative read budget for the read_artifact tool.
# Mapping ``file_id → chars already returned this turn``. Reset by the
# file_context pre_reply hook on every turn boundary so a single user turn
# can read up to ``MAX_FILE_CONTENT_CHARS`` per file via paginated calls.
#
# The contextvar pattern works because AgentScope dispatches tool calls in
# the same async task as the agent's reply(), so the value set in the hook
# propagates to read_artifact.
_artifact_read_chars_per_turn: ContextVar[Optional[Dict[str, int]]] = ContextVar(
    "_artifact_read_chars_per_turn", default=None
)

# Per-turn cache for the workspace-pin reminder hook:
#   seen: file_ids the agent has observed in tool results this turn
#   last_reminded_sig: signature of the unpinned set the last reminder covered
#     (used for in-hook dedup to avoid spamming when the same unpinned set
#     persists across multiple tool calls).
_pin_hint_state: ContextVar[Optional[Dict[str, Any]]] = ContextVar("_pin_hint_state", default=None)


def get_artifact_read_state() -> Dict[str, int]:
    """Return the current per-turn read tracker, lazily initializing it.

    Used by ``read_artifact`` to enforce the cumulative cap.
    """
    state = _artifact_read_chars_per_turn.get()
    if state is None:
        state = {}
        _artifact_read_chars_per_turn.set(state)
    return state


def reset_artifact_read_state() -> None:
    """Clear the per-turn read tracker. Called from file_context_pre_reply."""
    _artifact_read_chars_per_turn.set({})


def reset_pin_hint_state() -> None:
    """Clear the per-turn pin-reminder cache. Called at turn boundary."""
    _pin_hint_state.set({"seen": set(), "last_reminded_sig": ""})


def _get_pin_hint_state() -> Dict[str, Any]:
    state = _pin_hint_state.get()
    if state is None:
        state = {"seen": set(), "last_reminded_sig": ""}
        _pin_hint_state.set(state)
    return state


# ── DynamicModel helper (used by DynamicModelMiddleware) ─────────────────

# Cache key: main/provider + mode → model instance. fast and the three thinking tiers each get one cached entry.
_model_cache: dict[str, Any] = {}
_cached_version: int = -1


def _check_version():
    """Invalidate cached model instances when ModelConfigService version changes."""
    global _model_cache, _cached_version
    try:
        from core.services.model_config import ModelConfigService

        current = ModelConfigService.get_instance().version
    except Exception:
        return
    if current != _cached_version:
        _model_cache = {}
        _cached_version = current


def _get_main_model(mode: str = "medium"):
    """Get the main agent model for the given chat mode (fast/medium/high/max)."""
    from core.llm.chat_models import get_default_model

    _check_version()
    key = f"main:{mode}"
    cached = _model_cache.get(key)
    if cached is not None:
        return cached
    if mode in ("fast", "turbo"):
        instance = get_default_model(disable_thinking=True, stream=True)
    elif mode in ("high", "max"):
        instance = get_default_model(reasoning_effort=mode, stream=True)
    else:  # medium or unknown
        # medium uses the "thinking model", but picks chat_template_kwargs based on
        # supports_reasoning_effort (chat_models internally distinguishes
        # effort=medium vs None)
        instance = get_default_model(reasoning_effort="medium", stream=True)
    _model_cache[key] = instance
    return instance


def _get_provider_model(provider_id: str, mode: str = "medium"):
    """Get a user-selected active chat provider model for the given chat mode."""
    from core.llm.chat_models import make_chat_model
    from core.services.model_config import ModelConfigService

    pid = (provider_id or "").strip()
    if not pid:
        return _get_main_model(mode)
    _check_version()
    key = f"provider:{pid}:{mode}"
    cached = _model_cache.get(key)
    if cached is not None:
        return cached
    resolved = ModelConfigService.get_instance().resolve_provider(pid)
    if resolved is None:
        return _get_main_model(mode)

    supports_effort = bool((resolved.extra or {}).get("supports_reasoning_effort"))
    disable_thinking = mode in ("fast", "turbo")
    reasoning_effort = None
    if not disable_thinking and supports_effort and mode in ("medium", "high", "max"):
        reasoning_effort = mode

    instance = make_chat_model(
        model=resolved.model_name,
        temperature=resolved.temperature,
        max_tokens=resolved.max_tokens,
        timeout=resolved.timeout,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        provider=resolved.provider,
        provider_extra=resolved.provider_extra,
        disable_thinking=disable_thinking,
        reasoning_effort=reasoning_effort,
        stream=True,
        # Same per-model admin flag as get_default_model: reasoning arrives via the
        # separate reasoning_content channel, so announce it at stream start and the
        # frontend never buffers a no-thinking answer as presumed reasoning.
        structured_reasoning=(
            True if (resolved.extra or {}).get("structured_reasoning") else None
        ),
    )
    _model_cache[key] = instance
    return instance


def _resolve_chat_mode(ctx) -> str:
    """Resolve the final chat_mode (turbo/fast/medium/high/max) from agent.state (AgentRuntimeState)."""
    raw = getattr(ctx, "chat_mode", None)
    if raw in ("turbo", "fast", "medium", "high", "max"):
        return raw
    # Fallback for legacy clients
    return "medium" if getattr(ctx, "enable_thinking", True) else "fast"


# ── FileContext hook (replaces FileContextMiddleware) ─────────────────────


def _is_image(f: Dict[str, Any]) -> bool:
    mime = (f.get("mime_type") or "").lower()
    return mime.startswith("image/")


def _download_artifact_bytes(
    file_id: str,
    fallback_name: str,
    log_prefix: str,
    user_id: Optional[str] = None,
) -> Optional[bytes]:
    """Resolve an artifact's storage key and download its raw bytes.

    Returns None on any failure (missing artifact, storage error, etc.) and
    logs at WARNING. Used by the image and xlsx-preview hook branches that
    both need raw bytes (not parsed text).

    When ``user_id`` is supplied, the artifact's owner is verified via
    :func:`load_artifact_meta` before any storage round-trip. This is the
    defense-in-depth gate for hook-level reads: the agent context is built
    from ``request.attachments[].file_id`` (client-supplied), and the
    backend boundary doesn't re-verify ownership before reaching the
    hook. Without this check, a forged ``file_id`` belonging to another
    user would have its raw bytes downloaded and silently materialized
    into the agent's memory (image base64 or xlsx preview block). The
    matching ``fetch_parsed_text`` already does the same check for parsed
    text — this brings the binary paths to parity.

    ``user_id=None`` preserves the legacy "trust the caller" behavior
    used by ``core.llm.tools.read_artifact_tool._probe_xlsx_sheet_names`` /
    ``_probe_pptx_slide_count`` where ``fetch_parsed_text(user_id=...)``
    is the authoritative gate downstream.
    """
    from core.content.artifact_reader import (
        load_artifact_meta,
        resolve_artifact_storage,
    )
    from core.infra.exceptions import StorageError
    from core.storage import get_storage

    if user_id:
        meta = load_artifact_meta(file_id, user_id=user_id)
        if meta is None:
            logger.warning(
                "%s: artifact %s denied for user %s (not owner / missing / deleted)",
                log_prefix,
                file_id,
                user_id,
            )
            return None

    storage_key, _ = resolve_artifact_storage(file_id, fallback_name)
    if not storage_key:
        return None

    try:
        return get_storage().download_bytes(storage_key)
    except StorageError as e:
        logger.warning(f"{log_prefix}: storage download failed for {file_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"{log_prefix}: unexpected download error for {file_id}: {e}")
        return None


def _fetch_image_base64(
    f: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Fetch image data for a file attachment and return (base64_data, mime_type)."""
    file_id = f.get("file_id") or ""
    if not file_id:
        return None
    mime_type = (f.get("mime_type") or "image/png").lower()
    raw = _download_artifact_bytes(
        file_id,
        f.get("name") or "image",
        "Image hook",
        user_id=user_id,
    )
    if raw is None:
        return None
    return base64.b64encode(raw).decode("utf-8"), mime_type


def _is_xlsx(f: Dict[str, Any]) -> bool:
    """Detect xlsx by mime_type or filename extension.

    We only special-case xlsx (not xls/csv) because:
      - xlsx is the dominant batch-execution upload format
      - parse_xlsx_preview uses openpyxl, same engine as the batch path
      - xls would need a separate xlrd-based preview helper (out of scope)
    """
    mime = (f.get("mime_type") or "").lower()
    if "spreadsheetml.sheet" in mime:
        return True
    name = (f.get("name") or f.get("filename") or "").lower()
    return name.endswith(".xlsx")


def _build_xlsx_preview_block(
    f: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Optional[str]:
    """Build the bounded preview + metadata + directive block for a single xlsx.

    Returns None if the file can't be read (caller falls back to the regular
    parsed_text path, so the agent at least sees *something*).
    """
    from core.content.file_parser import parse_xlsx_preview

    file_id = f.get("file_id") or ""
    name = f.get("name") or f.get("filename") or "未命名表格"
    if not file_id:
        return None

    file_bytes = _download_artifact_bytes(
        file_id,
        name,
        "xlsx preview",
        user_id=user_id,
    )
    if file_bytes is None:
        return None

    try:
        from core.content.artifact_reader import (
            ATTACHMENT_PREVIEW_MAX_CHARS,
            fetch_parsed_text,
        )

        # Reuse the bytes already downloaded for the structural preview to
        # populate parsed_text + summary without a second storage round-trip.
        fetch_parsed_text(file_id, user_id=user_id, prefetched_bytes=file_bytes)
        info = parse_xlsx_preview(file_bytes, char_budget=ATTACHMENT_PREVIEW_MAX_CHARS)
    except RuntimeError as e:
        logger.warning(f"xlsx preview: parse failed for {file_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"xlsx preview: unexpected parse error for {file_id}: {e}")
        return None

    total_rows = info["total_rows"]
    total_cols = info["total_columns"]
    preview_rows = info["preview_rows"]
    omitted = max(0, total_rows - preview_rows)
    cols_str = ", ".join(info["header"]) if info["header"] else "(无列名)"

    lines: List[str] = [
        f"[文件: {name}]",
        f"  file_id: {file_id}",
        f"  总规模: {total_rows} 行 × {total_cols} 列",
        f"  列名: [{cols_str}]",
        f"  已展示: 前 {preview_rows} 行（约 {len(info['preview_md'])} 字符，达到预览预算）",
    ]
    if info.get("truncated_columns"):
        lines.append(f"  注意: 共 {total_cols} 列，预览仅含前 {len(info['header'])} 列")
    if info.get("truncated_cells"):
        lines.append(f"  注意: {info['truncated_cells']} 个超长单元格已截断到 500 字以内")
    lines.append("")
    lines.append(info["preview_md"])
    if omitted:
        lines.append(f"... (剩余 {omitted} 行未展示) ...")
    lines.append("")
    lines.append("[操作指引]")
    lines.append(
        f"- 需要预览之外的正文时，调用 read_artifact(file_id='{file_id}', "
        "offset=N, limit=4000) 按需读取"
    )
    lines.append("- 以上只是有界预览，不要把未展示的行当作不存在，也不要推断未读取内容")
    return "\n".join(lines)


def _build_file_context(
    uploaded_files: List[Dict[str, Any]],
    user_id: Optional[str] = None,
) -> str:
    """Assemble the text-attachment list into the context text injected into the model (non-image files only).

    Files are resolved exclusively by ``file_id``. The backend lazily parses and
    caches the artifact, then injects only metadata plus a bounded preview. The
    request body never supplies parsed content.

    ``user_id`` is used as the ownership check when fetching ``fetch_parsed_text`` /
    the xlsx preview, preventing a forged request-body ``attachments[].file_id``
    from reading across users — see the :func:`_download_artifact_bytes` docstring
    for the core defense point.
    """
    text_files = [f for f in uploaded_files if not _is_image(f)]
    if not text_files:
        return ""

    file_descriptions = []
    for f in text_files:
        name = f.get("name", "未知文件")
        fid = f.get("file_id", "")
        desc = f"- {name}"
        if fid:
            desc += f"  (file_id: {fid})"
        file_descriptions.append(desc)
    from core.content.artifact_reader import (
        ATTACHMENT_PREVIEW_MAX_CHARS,
        fetch_parsed_text,
    )

    file_content_parts: List[str] = []
    for f in text_files:
        # ── xlsx: bounded preview + metadata + directive (no full content) ──
        if _is_xlsx(f):
            block = _build_xlsx_preview_block(f, user_id=user_id)
            if block is not None:
                file_content_parts.append(block)
                continue
            # Fall through to regular path on preview failure so the agent
            # at least sees the parsed markdown (degraded but non-fatal).

        fid = (f.get("file_id") or "").strip()
        mime_type = f.get("mime_type") or ""
        if not fid:
            file_content_parts.append(
                f"[文件: {f.get('name', '未知文件')}]\n"
                "  状态: 缺少 file_id，无法读取正文"
            )
            continue

        logger.info("Document hook: loading preview for %s (%s)", f.get("name"), fid)
        content = fetch_parsed_text(fid, user_id=user_id)
        if not content:
            file_content_parts.append(
                f"[文件: {f.get('name', '未知文件')}]\n"
                f"  file_id: {fid}\n"
                f"  MIME: {mime_type or '(未知)'}\n"
                "  状态: 文件不可访问或解析失败"
            )
            continue

        total_chars = len(content)
        preview = content[:ATTACHMENT_PREVIEW_MAX_CHARS]
        truncated = total_chars > len(preview)
        block = [
            f"[文件: {f.get('name', '未知文件')}]",
            f"  file_id: {fid}",
            f"  MIME: {mime_type or '(未知)'}",
            f"  解析文本: 共 {total_chars} 字符，本轮自动展示 {len(preview)} 字符",
            "",
            preview,
        ]
        if truncated:
            block.append(f"... (其余 {total_chars - len(preview)} 字符未展示) ...")
        block.extend(
            [
                "",
                "[读取指引]",
                f"- 需要未展示的正文时，调用 read_artifact(file_id='{fid}', offset=N, limit=4000)",
                "- 不要推断尚未读取的内容",
            ]
        )
        file_content_parts.append("\n".join(block))
    file_content = "\n\n---\n\n".join(file_content_parts)

    return (
        f"[file name]: {chr(10).join(file_descriptions)}\n"
        f"[file content begin]\n"
        f"{file_content}\n"
        f"[file content end]\n"
        "以上是用户附件的元数据和有界预览；需要预览之外的正文时必须按需调用 read_artifact。"
    )


def _source_label(source: str) -> str:
    from core.content.artifact_reader import SOURCE_AI_GENERATED, SOURCE_USER_UPLOAD

    return {SOURCE_USER_UPLOAD: "用户上传", SOURCE_AI_GENERATED: "AI 生成"}.get(source, "")


def _build_historical_files_context(historical_files: List[Dict[str, Any]]) -> str:
    """Build a compact summary block for files from previous turns.

    Includes both user-uploaded files and AI-generated files (reports,
    charts, code output, etc.) produced by tools in earlier turns.
    Each entry is labeled by provenance so the agent knows what kind of
    file it's referencing.

    Agent gets only `{filename, file_id, source, summary}`; full content
    is fetched on-demand via the `read_artifact` tool. This keeps prompt
    size bounded as the conversation grows.
    """
    if not historical_files:
        return ""

    lines: List[str] = []
    for f in historical_files:
        file_id = f.get("file_id") or ""
        name = f.get("name") or f.get("filename") or "未命名文件"
        mime = f.get("mime_type") or ""
        summary = (f.get("summary") or "").strip()
        source = f.get("source") or ""
        deleted = bool(f.get("deleted"))

        source_label = _source_label(source)
        header_parts = [f"file_id: {file_id}"]
        if source_label:
            header_parts.append(source_label)
        if mime:
            header_parts.append(mime)
        header = f"- {name}  ({', '.join(header_parts)})"

        if deleted:
            lines.append(header + "  [文件已删除，无法读取]")
            continue

        lines.append(header)
        # Key point: tell the model the stable /myspace/<name> path so it can operate
        # directly with Read/Edit/Write — better suited than read_artifact for
        # "modify HTML/CSS/MD/source code" text-editing scenarios, and changes sync
        # back to "My Space" immediately.
        if name and source == _ai_source():
            lines.append(f"  沙盒路径: /myspace/{name}   ← 用 Read/Edit/Write 直接读改写")
        if summary:
            indented = "\n".join(f"  {ln}" for ln in summary.splitlines())
            lines.append(f"  摘要：\n{indented}")
        else:
            lines.append("  摘要：（尚未生成）")

    header = (
        "[历史文件清单]（本会话中之前轮次涉及的文件，包含用户上传和 AI 生成的两类，以下仅为摘要）\n"
        "访问方式：\n"
        '- **要修改文本/HTML/MD/代码** → 用 `Read("/myspace/<filename>")` 读完整内容，'
        "再用 `Edit` / `Write` 改；改完会自动同步回「我的空间」（同一 file_id）。\n"
        "- **要看 docx/pdf/xlsx 等需要解析的二进制文件** → 用 "
        "`read_artifact(file_id, offset, limit)` 拿解析后的文本。"
    )
    return header + "\n\n" + "\n".join(lines)


def _ai_source() -> str:
    """Lazy resolve to avoid circular import at module load."""
    from core.content.artifact_reader import SOURCE_AI_GENERATED

    return SOURCE_AI_GENERATED


# ── WorkspacePinHint hook ─────────────────────────────────────────────────
# Strict-mode `pin_to_workspace` gating means generated files are hidden
# from users until the agent explicitly pins them. Multiple input paths
# (MCP tools that return file_id, `sandbox_get_artifact` for bash-produced
# files, skill scripts) all share this responsibility, and the model
# (especially smaller / flash variants under long contexts) often forgets
# the pin step — see trace aeb643c6-5a8f-4874-a4b4-3d472c581fa6 where 6
# generate_chart_tool + excel_create_workbook + pdf_create + excel_export_pdf
# produced 8 file_ids, all delivered to the user as exactly zero files.
#
# This `post_acting` hook scans every tool result for `"file_id"` mentions,
# diffs them against the workspace state's pinned set, and appends a
# concise system reminder to memory listing the unpinned ids. The reminder
# is internal (added directly via `memory.add`, not via `print`) so it
# doesn't surface to the SSE stream.

# Pattern picks up top-level `"file_id": "<id>"` keys in tool result JSON.
# We deliberately match the JSON key form so we ignore `artifact_id`-style
# echoes from `list_myspace_files` / `sandbox_put_artifact` etc., which
# operate on already-existing artifacts and don't need pinning.
_FILE_ID_RE = re.compile(r'"file_id"\s*:\s*"([A-Za-z0-9_\-]+)"')

# Tools whose results legitimately mention `file_id` but never produce a
# new deliverable artifact — skipping them avoids spurious reminders.
_PIN_HINT_SKIP_TOOLS: frozenset[str] = frozenset(
    {
        "pin_to_workspace",  # the pin tool itself
        "read_artifact",  # echoes the input file_id; reading, not producing
        "generate_response",  # the ReActAgent finish function
    }
)


# ── Goal-anchor reminder hook ─────────────────────────────────────────────
# Midway through a long-context ReAct task, the model's grip on the user's original
# constraints gets diluted by the project file listing + intermediate tool results.
# Borrowing the Claude Code TODO_REMINDER idea: periodically re-inject the original
# prompt into memory to give the model a chance to reflect. This system has no
# TodoWrite, so the reminder can only make the model self-assess against the
# "original prompt"; to compensate for the "no checklist to reconcile against"
# weakness, we also force one injection on the first "output-producing" tool call
# (catching the last window where "the model starts writing but drops a user
# requirement").
_GOAL_ANCHOR_WARMUP_CALLS = 3
_GOAL_ANCHOR_INTERVAL = 10
# These tools hint the model is about to "land" its existing plan into an artifact
# (markdown draft, bash running word-cli, etc.). Hitting any of them forces one
# injection (at most 1 forced per reply turn).
# Not included: sandbox_get_artifact / pin_to_workspace (already on the delivery
# path; reminding again confuses the model into going silent and quitting —
# empirically on 2026-05-26 the pin rate went 92% → 0%);
# sandbox_put_artifact (INPUT direction).
_GOAL_ANCHOR_OUTPUT_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "Write",
        "write_text_file",
    }
)

_GOAL_ANCHOR_REMINDER_TEMPLATE = """用户原始请求：
> {original}

对照一下：你目前获取到的内容、即将产出的东西，跟用户原始请求**完整对得上吗**？有没有漏掉用户列出的某一项？"""


# ── Plan-staleness reminder hook（update_plan 催更）────────────────────────
# 计划栏往前走，全靠模型在同一轮里反复调用 ``update_plan``；而这条要求只在系统提示词
# 开头讲一次。长任务跑十几轮工具之后，这条指令早被中间结果冲淡了——线上实测一轮 20 次
# LLM 往返、产出两个 PPTX 的任务，``update_plan`` 只在开头调了两次，用户看到的计划栏
# 从头到尾停在 1/5。workflow 末尾那道兜底只补"没有 pending、只剩一个 in_progress"的
# 收尾场景，救不了半路就不动的清单。
#
# 所以按 Goal-anchor 同一套思路，每隔几轮把**当前清单原样回灌**给模型：让"更新清单"
# 退化成照着抄一遍状态的机械动作，而不是要模型自己想起来还有这么个工具。回灌频率比
# goal-anchor 高得多（3 轮 vs 10 轮），因为计划栏是用户全程盯着的实时进度条，晚一轮
# 更新用户就多看一轮错的数字。
_PLAN_STALE_INTERVAL = 3

_PLAN_STALE_REMINDER_TEMPLATE = """当前任务计划清单（{done}/{total} 已完成，已有 {rounds} 轮没有更新）：
{checklist}

对照你刚做完的事：如果其中某一步已经完成，**立刻调用 `update_plan`** 传入全量步骤列表，把
完成的标成 completed、正在做的标成 in_progress（始终保持恰好一个 in_progress）；计划有变就
顺带增删步骤。用户输入框上方的计划栏就是照这份清单渲染的，不更新它用户看到的进度就是错的。
调用 `update_plan` 不会打断你——更新完继续执行手上的活，不要停下来等待。"""
