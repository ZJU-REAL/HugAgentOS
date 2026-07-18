"""Runtime hooks for pluggable prompt/tools.

This file defines the main integration boundaries so later we can swap in
alternative prompt builders or tool routers.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Dict, Iterable, List, Optional, Tuple

from prompts.prompt_config import PromptConfig
from prompts.provider import (
    FilesystemPromptProvider,
    InlinePromptProvider,
    hardcoded_minimal_system_prompt,
)

# ── System prompt TTL cache ──────────────────────────────────────────────
_PROMPT_CACHE_TTL = 300.0  # seconds
_prompt_cache_lock = Lock()
# key -> (expires_at, prompt_template_without_now)
_prompt_cache: Dict[tuple, Tuple[float, str]] = {}


_db_version_cache_lock = Lock()
_db_version_cache: Optional[Tuple[float, str]] = None
_DB_VERSION_CACHE_TTL = 30.0  # seconds

# ── Pre-loaded DB prompt parts (populated by warmup, invalidated on change) ──
_db_parts_preloaded_lock = Lock()
_db_parts_preloaded: Optional[Dict[str, Dict[str, Any]]] = None


def _get_db_prompt_version() -> str:
    """Return MAX(updated_at) from admin_prompt_parts as a cache-busting version string.

    Cached for 30s to avoid hitting DB on every build_system_prompt call.
    Invalidated alongside the prompt cache by _invalidate_prompt_cache().
    """
    global _db_version_cache
    now = monotonic()
    with _db_version_cache_lock:
        if _db_version_cache is not None:
            expires_at, val = _db_version_cache
            if now < expires_at:
                return val

    try:
        from sqlalchemy import func
        from core.db.engine import SessionLocal
        from core.db.models import AdminPromptPart
        db = SessionLocal()
        try:
            result = db.query(func.max(AdminPromptPart.updated_at)).scalar()
            val = result.isoformat() if result else ""
        finally:
            db.close()
    except Exception:
        val = ""

    with _db_version_cache_lock:
        _db_version_cache = (now + _DB_VERSION_CACHE_TTL, val)
    return val


def _load_db_prompt_parts() -> Dict[str, Dict[str, Any]]:
    """Load prompt part overrides from DB.

    Returns pre-loaded cache if available (populated by warmup_prompt_cache),
    otherwise falls back to a live DB query. Returns empty dict on failure.
    """
    with _db_parts_preloaded_lock:
        if _db_parts_preloaded is not None:
            return _db_parts_preloaded

    return _fetch_db_prompt_parts()


def _fetch_db_prompt_parts() -> Dict[str, Dict[str, Any]]:
    """Direct DB query for prompt parts. Always hits the database."""
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminPromptPart
        db = SessionLocal()
        try:
            # Deterministic order: DB-only parts are concatenated into the system prompt
            # in this dict's iteration order; without ORDER BY, Postgres row order can
            # drift → busting the LLM prefix cache.
            rows = (
                db.query(AdminPromptPart)
                .order_by(AdminPromptPart.sort_order, AdminPromptPart.part_id)
                .all()
            )
            return {
                r.part_id: {
                    "content": r.content,
                    "sort_order": r.sort_order,
                    "is_enabled": r.is_enabled,
                }
                for r in rows
            }
        finally:
            db.close()
    except Exception:
        return {}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def warmup_prompt_cache() -> None:
    """Pre-load DB prompt parts and version at startup.

    Call this during application startup so that the first chat request
    does not need to query the database for prompt parts.

    Also seeds the project-mode part along the way, so the Config admin UI can
    see / edit that entry on its very first load.
    """
    global _db_parts_preloaded
    import logging
    log = logging.getLogger(__name__)

    # Project-mode section: insert the default if missing in the DB (idempotent). Must
    # run before _fetch, otherwise on first startup the cache lacks this entry and
    # project_id chats fall back to the Python default instead of the DB template.
    ensure_project_mode_part_seeded()
    # System-reminder convention section: insert the default if missing in the DB
    # (idempotent). Teaches the model that out-of-band <system-reminder> markers exist
    # and how to handle them.
    ensure_system_reminder_convention_seeded()

    parts = _fetch_db_prompt_parts()
    with _db_parts_preloaded_lock:
        _db_parts_preloaded = parts

    # Also warm the version cache
    _get_db_prompt_version()
    log.info("[prompt_cache] Warmed up: %d DB prompt parts loaded", len(parts))


def invalidate_prompt_cache() -> None:
    """Clear all prompt caches so changes take effect on next request.

    Call this after admin prompt edits, skill toggles, or catalog changes.
    """
    global _db_parts_preloaded, _db_version_cache
    import logging
    log = logging.getLogger(__name__)

    with _db_parts_preloaded_lock:
        _db_parts_preloaded = None
    with _db_version_cache_lock:
        _db_version_cache = None
    with _prompt_cache_lock:
        _prompt_cache.clear()
    invalidate_kb_lite_cache()

    # Also drop prompt_version_service cached payload
    try:
        from core.services import prompt_version_service as pvs
        pvs.invalidate_cache()
    except Exception:
        pass

    # Re-populate the preloaded cache immediately so the next request is fast
    parts = _fetch_db_prompt_parts()
    with _db_parts_preloaded_lock:
        _db_parts_preloaded = parts
    _get_db_prompt_version()

    log.info("[prompt_cache] Invalidated and re-warmed: %d DB prompt parts", len(parts))


_BACKEND_ROOT = Path(__file__).resolve().parents[1]

# KB-lite section moved to prompts.kb_lite_section; re-export for compat
from prompts.kb_lite_section import invalidate_kb_lite_cache, _build_kb_lite_section  # noqa: E402


_TOOLS_AND_SKILLS_NOTICE = (
    "## 工具与技能\n\n"
    "当前已为你注入若干 MCP 工具，每个工具的适用场景、与其他工具的取舍、"
    "关键参数都写在其 description 字段里——选工具时请认真阅读 description "
    "里的中文「何时使用 / 何时改用别的工具」段落。\n\n"
    "除 MCP 工具外，系统还提供 **Agent Skills**（技能），列在下方。"
    "处理请求时先匹配技能描述；没有匹配技能时，再直接调用最合适的 MCP 工具。"
)


def build_subagent_system_prompt(
    user_agent: Any,
    tool_schemas: list,
    enabled_mcp_keys: list[str],
    enabled_kb_ids: Optional[list[str]] = None,
) -> str:
    """Build the system prompt for a subagent.

    Structure:
    1. User-defined system_prompt (core role definition)
    2. Tool usage policy (20_tools_policy)
    3. Citation rules (65_citations)
    4. Output format (60_format)
    5. Tool routing table (dynamically generated)
    6. Lightweight KB catalog (if any)
    7. Time info (**deliberately last**: the date is the only day-varying content
       in the prompt; putting it at the tail lets the long preceding prefix hit
       the LLM prefix cache across day boundaries)
    """
    # Day granularity only: the date is constant within a day → the system prompt is
    # byte-stable all day → LLM prefix cache hits all day (a second-level timestamp
    # would change every request and bust the cache).
    now = datetime.now().strftime("%Y-%m-%d")

    # Read core prompt segments from filesystem (fallback path)
    prompt_dir_cfg = os.getenv("PROMPT_DIR") or "./prompts/prompt_text/default"
    prompt_dir = _resolve_prompt_dir(prompt_dir_cfg)
    fs = FilesystemPromptProvider(prompt_dir=prompt_dir, strict_vars=False)

    # Prefer active version's parts when available (map by part_id suffix)
    _active_parts: Dict[str, str] = {}
    try:
        from core.services import prompt_version_service as pvs
        _av = pvs.get_active_version("system")
        if _av:
            for p in _av.get("parts") or []:
                if not p.get("is_enabled", True):
                    continue
                _active_parts[(p.get("part_id") or "").strip()] = p.get("content") or ""
    except Exception:
        pass

    def _load_segment(key: str) -> str:
        """Prefer the active version's part; fall back to filesystem."""
        pid = f"system/{key}"
        if pid in _active_parts:
            return _active_parts[pid]
        return fs.get_prompt(key, "system", vars={"now": now})

    segments: List[str] = []

    # 1. User-defined system prompt (core role)
    custom_prompt = (user_agent.system_prompt or "").strip()
    if custom_prompt:
        segments.append(f"## 角色设定\n{custom_prompt}")

    # 3. Tools policy
    tools_policy = _load_segment("20_tools_policy")
    if tools_policy.strip():
        segments.append(tools_policy.strip())

    # 4. Citations
    citations = _load_segment("65_citations")
    if citations.strip():
        segments.append(citations.strip())

    # 5. Output format
    fmt = _load_segment("60_format")
    if fmt.strip():
        segments.append(fmt.strip())

    # 6. Tools/skills notice
    if tool_schemas:
        segments.append(_TOOLS_AND_SKILLS_NOTICE)

    # 7. Lightweight KB catalog
    if enabled_kb_ids:
        kb_section = _build_kb_lite_section(enabled_kb_ids)
        if kb_section:
            segments.append(kb_section)

    # 8. Time info — last on purpose: the date is the only day-varying bytes
    # in this prompt; keeping it at the tail preserves the long shared prefix
    # across day boundaries for LLM prefix caching.
    segments.append(f"## 当前时间\n{now}")

    return "\n\n".join(segments)


def _resolve_prompt_dir(config_prompt_dir: str) -> Path:
    raw_prompt_dir = os.getenv("PROMPT_DIR") or config_prompt_dir
    path = Path(raw_prompt_dir)
    if path.is_absolute():
        return path

    # Preserve existing behavior first: resolve relative to current working directory.
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path

    # Also support launching from repo root while config uses backend-relative paths.
    backend_path = _BACKEND_ROOT / path
    if backend_path.exists():
        return backend_path

    return cwd_path


def _extract_tool_names(tools) -> Tuple[str, ...]:
    """Extract sorted tool names for cache key construction."""
    names = []
    for tool in (tools or []):
        name = getattr(tool, "name", None)
        if not name and isinstance(tool, dict):
            func_info = tool.get("function", {})
            name = func_info.get("name") if isinstance(func_info, dict) else None
        if name:
            names.append(name)
    return tuple(sorted(names))


# Project-mode section moved to prompts.project_section; re-export for compat
from prompts.project_section import (  # noqa: E402
    _format_size, _PROJECT_FILE_LIST_CAP, PROJECT_MODE_PART_ID, PROJECT_MODE_DISPLAY_NAME,
    _PROJECT_MODE_DEFAULT_TEMPLATE, _render_file_list_block, _render_folder_scope_block,
    _render_instructions_block, _collapse_blanks, _get_project_mode_template, _build_project_section,
)
SYSTEM_REMINDER_CONVENTION_PART_ID = "system/05_system_reminder_convention"
SYSTEM_REMINDER_CONVENTION_DISPLAY_NAME = "05_system_reminder_convention"
_SYSTEM_REMINDER_CONVENTION_DEFAULT = """## 系统消息中的 <system-reminder> 标记

对话中你会看到 `<system-reminder>...</system-reminder>` 包裹的系统提醒。这些是
**带外信号**，由系统自动注入，**与具体的工具结果或用户消息没有直接关系**。它们
用来：
- 同步当前任务进度
- 提示你被遗忘的约束（原始用户目标、未完成的待办、未交付的文件）
- 在你即将做某个动作前给出额外的轻量提示

**处理原则（重要）：**
- system-reminder 的优先级**高于一般对话上下文**，但**低于用户原始请求**
- **绝不**在回复正文里向用户提及收到了 system-reminder（如"我注意到系统提醒……"
  "根据系统提示……"这类表述一律禁止）
- **绝不**因为收到 system-reminder 就中止当前 reply turn，也**不要**回复用户
  征求确认（如"是否继续？""您看这样可以吗？"）。reminder 的作用是辅助你做
  **下一次工具调用**的决策，而不是中断当前任务
- 如果 reminder 提示你已偏离原始目标，**调整下一次工具调用的参数或换用更合适
  的工具**纠正方向；不要把已经跑了一半的错方向硬走完，也不要中止本轮回复
"""


def ensure_system_reminder_convention_seeded() -> None:
    """Called once at startup: if the active 'system' version's parts lack
    ``system/05_system_reminder_convention``, insert the default.

    Same pattern as ``ensure_project_mode_part_seeded``: if it already exists in the
    DB (regardless of enabled state) leave it alone; if an admin deletes it via the
    UI, the next startup re-seeds it (treated as restoring the default).

    sort_order=5: placed after ``system/00_role`` and before ``system/10_constraints``,
    so the model sees the "system conventions" before the anti-hallucination constraints.
    """
    import logging
    log = logging.getLogger(__name__)
    try:
        from core.services import prompt_version_service as pvs
        try:
            pvs.seed_from_filesystem()
        except Exception:
            pass
        active = pvs.get_active_version("system")
        if not active or not active.get("id"):
            log.warning(
                "[prompt_seed] no active system version; skipped %s seed",
                SYSTEM_REMINDER_CONVENTION_PART_ID,
            )
            return
        parts = list(active.get("parts") or [])
        if any(
            (p.get("part_id") or "").strip() == SYSTEM_REMINDER_CONVENTION_PART_ID
            for p in parts
        ):
            return  # already present, idempotent return
        parts.append({
            "part_id": SYSTEM_REMINDER_CONVENTION_PART_ID,
            "content": _SYSTEM_REMINDER_CONVENTION_DEFAULT,
            "display_name": SYSTEM_REMINDER_CONVENTION_DISPLAY_NAME,
            "sort_order": 5,
            "is_enabled": True,
        })
        pvs.upsert_version(
            "system",
            active["id"],
            name=active.get("name"),
            description=active.get("description"),
            parts=parts,
        )
        log.info(
            "[prompt_seed] seeded %s into active system version=%s",
            SYSTEM_REMINDER_CONVENTION_PART_ID, active["id"],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "[prompt_seed] ensure_system_reminder_convention_seeded skipped: %s",
            exc,
        )


def ensure_project_mode_part_seeded() -> None:
    """Called once at startup: if the active 'system' version's parts lack project_mode, insert the default.

    The Config admin prompts/parts list reads the active version's parts, so the seed
    must land there to show up in the UI. Idempotent: if it already exists (whether
    enabled/disabled/admin-edited) it is left alone. If an admin deletes it via the UI,
    the next startup re-seeds it — treated as "restore default". Silent on failure.
    """
    import logging
    log = logging.getLogger(__name__)
    try:
        from core.services import prompt_version_service as pvs
        # First make sure the active version exists (first cold start seeds from filesystem md)
        try:
            pvs.seed_from_filesystem()
        except Exception:
            pass
        active = pvs.get_active_version("system")
        if not active or not active.get("id"):
            log.warning("[prompt_seed] no active system version; skipped project_mode seed")
            return
        parts = list(active.get("parts") or [])
        if any((p.get("part_id") or "").strip() == PROJECT_MODE_PART_ID for p in parts):
            return  # already present, idempotent return
        max_order = max((int(p.get("sort_order") or 0) for p in parts), default=0)
        parts.append({
            "part_id": PROJECT_MODE_PART_ID,
            "content": _PROJECT_MODE_DEFAULT_TEMPLATE,
            "display_name": PROJECT_MODE_DISPLAY_NAME,
            # Placed after all existing parts; stands alone as a "dynamic appendix section" in the UI list
            "sort_order": max(max_order + 100, 9000),
            "is_enabled": True,
        })
        pvs.upsert_version(
            "system",
            active["id"],
            name=active.get("name"),
            description=active.get("description"),
            parts=parts,
        )
        log.info(
            "[prompt_seed] seeded %s into active system version=%s",
            PROJECT_MODE_PART_ID, active["id"],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[prompt_seed] ensure_project_mode_part_seeded skipped: %s", exc)


def build_system_prompt(config: PromptConfig, ctx: Dict[str, Any] | None = None) -> str:
    """Build the system prompt from config + runtime context.

    Results are cached with a 300s TTL. The {now} placeholder is replaced
    at render time so the cache isn't invalidated every second.

    Adds a *dynamic* appendix describing currently-available tools (name + short description)
    so tools remain pluggable without hardcoding tool names in the static prompt.

    Fallback order:
      1) Filesystem prompt (config.system_prompt.prompt_dir / env PROMPT_DIR)
      2) Inline template (config.system_prompt.inline_template or env PROMPT_INLINE_TEMPLATE)
      3) Minimal hardcoded fallback (guarantee non-empty)

    Args:
        config: Loaded PromptConfig.
        ctx: Runtime context (optional). Recognized keys:
            - now: override timestamp
            - tools: optional iterable of tool objects (each with `.name` and `.description`)
            - mcp_servers: list of enabled MCP server keys
    Returns:
        A non-empty system prompt string.
    """

    ctx = ctx or {}
    # Day granularity only: the date is constant within a day → the system prompt is
    # byte-stable all day → LLM prefix cache hits all day (a second-level timestamp
    # would change every request and bust the cache).
    now = ctx.get("now") or datetime.now().strftime("%Y-%m-%d")

    # Build cache key from stable inputs (excluding {now})
    prompt_dir_cfg = getattr(config.system_prompt, "prompt_dir", None) or "./prompts/prompt_text/default"
    parts_key = tuple(config.system_prompt.parts) if config.system_prompt.parts else ()
    tool_names = _extract_tool_names(ctx.get("tools"))
    mcp_keys = tuple(sorted(ctx.get("mcp_servers") or []))
    provider_key = (getattr(config.system_prompt, "provider", None) or "filesystem").strip().lower()
    enabled_kbs_key = tuple(sorted(ctx.get("enabled_kbs") or []))
    # Project mode goes into the cache key — system prompts for different projects
    # mounted in the same process must be cached separately.
    # File-list signature: (total count, tuple of the first N files' (artifact_id, name)) — adding/removing files changes the key
    _pf_raw = ctx.get("project_files") or []
    _pf_sig = tuple(
        (str(it.get("artifact_id") or ""), str(it.get("name") or ""), int(it.get("size_bytes") or 0))
        for it in _pf_raw[:50]
    )
    project_key = (
        str(ctx.get("project_id") or ""),
        (ctx.get("project_instructions") or "")[:200],
        str(ctx.get("project_folder_name") or ""),
        str(ctx.get("project_folder_kind") or ""),
        len(_pf_raw),
        _pf_sig,
    )

    # Active pool version (preferred source) — bust cache on change
    active_version_key: tuple[str, str] = ("", "")
    try:
        from core.services import prompt_version_service as pvs
        _av = pvs.get_active_version("system")
        if _av:
            active_version_key = (str(_av.get("id") or ""), str(_av.get("updated_at") or ""))
    except Exception:
        pass

    # Include DB prompt parts version in cache key for invalidation
    db_version = _get_db_prompt_version()
    cache_key = (
        provider_key, str(prompt_dir_cfg), parts_key, tool_names, mcp_keys,
        db_version, enabled_kbs_key, active_version_key, project_key,
    )

    # Check cache
    with _prompt_cache_lock:
        cached = _prompt_cache.get(cache_key)
        if cached is not None:
            expires_at, template = cached
            if monotonic() < expires_at:
                return template.replace("{now}", now)
            else:
                _prompt_cache.pop(cache_key, None)

    # Cache miss — build the prompt
    strict_vars = _env_bool("PROMPT_STRICT_VARS", True)

    # Use a placeholder for {now} so we can cache the template
    _NOW_PLACEHOLDER = "__PROMPT_NOW_PLACEHOLDER__"

    provider = provider_key
    base = ""

    # ── Try DB-backed prompt parts first ──────────────────────────────
    db_parts = _load_db_prompt_parts()

    # ── Preferred source: active version in prompt_version_service ────
    # When a version is active in ContentBlock(id=prompt_versions), its parts
    # are the source of truth. AdminPromptPart overlay still wins on part_id
    # collision for backward compatibility with rows created by the old admin UI.
    active_system_version: Optional[Dict[str, Any]] = None
    try:
        from core.services import prompt_version_service as pvs
        active_system_version = pvs.get_active_version("system")
    except Exception:
        active_system_version = None

    if active_system_version and active_system_version.get("parts"):
        from prompts.provider import render_template
        chunks: List[str] = []
        seen_ids: set[str] = set()
        for p in active_system_version["parts"]:
            pid = (p.get("part_id") or "").strip()
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            # project_mode is a dynamic appendix section (injected only for project chats); never enters the base prompt
            if pid == PROJECT_MODE_PART_ID:
                continue
            # DB override wins if present
            db_row = db_parts.get(pid)
            if db_row:
                if not db_row.get("is_enabled", True):
                    continue
                content = db_row["content"]
            else:
                if not p.get("is_enabled", True):
                    continue
                content = p.get("content") or ""
            txt = render_template(content, vars={"now": _NOW_PLACEHOLDER, **ctx}, strict=False)
            if txt.strip():
                chunks.append(txt.strip())
        # Include any DB-only parts not listed in the active version
        for pid, db_row in db_parts.items():
            if pid in seen_ids or not db_row.get("is_enabled", True):
                continue
            if pid == PROJECT_MODE_PART_ID:
                continue  # same as above: the dynamic appendix section never enters base
            txt = render_template(db_row["content"], vars={"now": _NOW_PLACEHOLDER, **ctx}, strict=False)
            if txt.strip():
                chunks.append(txt.strip())
        base = "\n\n".join(chunks).strip()

    # 1) Filesystem prompt: config-driven prompt pack.
    if (not base.strip()) and provider == "filesystem":
        prompt_dir = _resolve_prompt_dir(str(prompt_dir_cfg))
        fs_provider = FilesystemPromptProvider(prompt_dir=prompt_dir, strict_vars=strict_vars)

        parts = getattr(config.system_prompt, "parts", None)
        if isinstance(parts, list) and parts:
            # Build merged parts list: filesystem + DB-only parts
            all_part_ids = list(parts)
            for pid in db_parts:
                if pid not in all_part_ids:
                    all_part_ids.append(pid)

            # Sort by DB sort_order if available, else filesystem index * 10
            def _sort_key(pid: str) -> int:
                if pid in db_parts:
                    return db_parts[pid]["sort_order"]
                try:
                    return parts.index(pid) * 10
                except ValueError:
                    return 9999

            sorted_ids = sorted(all_part_ids, key=_sort_key) if db_parts else parts

            chunks: List[str] = []
            for part_id in sorted_ids:
                part_id_str = part_id.strip() if isinstance(part_id, str) else ""
                if not part_id_str:
                    continue
                # Dynamic appendix section: only injected for project chats by _build_project_section; never enters base
                if part_id_str == PROJECT_MODE_PART_ID:
                    continue

                db_row = db_parts.get(part_id_str)
                if db_row:
                    # DB override: check is_enabled
                    if not db_row["is_enabled"]:
                        continue
                    txt = db_row["content"]
                    # Apply variable substitution
                    from prompts.provider import render_template
                    txt = render_template(txt, vars={"now": _NOW_PLACEHOLDER, **ctx}, strict=False)
                else:
                    txt = fs_provider.get_prompt(part_id_str, "system", vars={"now": _NOW_PLACEHOLDER, **ctx})

                if txt.strip():
                    chunks.append(txt.strip())
            base = "\n\n".join(chunks).strip()
        else:
            # Backward compatible single-file convention: system.system.md
            base = fs_provider.get_prompt("system", "system", vars={"now": _NOW_PLACEHOLDER, **ctx})

    # 2) Inline prompt.
    if (not base.strip()) and provider == "inline":
        inline_provider = InlinePromptProvider(
            template=(getattr(config.system_prompt, "inline_template", "") or os.getenv("PROMPT_INLINE_TEMPLATE", "")),
            strict_vars=strict_vars,
        )
        base = inline_provider.get_prompt("system", "system", vars={"now": _NOW_PLACEHOLDER, **ctx})

    # 3) Absolute minimal fallback (guarantee non-empty).
    if not base.strip():
        base = hardcoded_minimal_system_prompt().strip()

    tools = ctx.get("tools")

    if tools:
        base = (base + "\n\n" + _TOOLS_AND_SKILLS_NOTICE).strip()

    # ── Lightweight KB catalog (name + description only) ──
    enabled_kbs = ctx.get("enabled_kbs")
    if enabled_kbs:
        kb_section = _build_kb_lite_section(enabled_kbs)
        if kb_section:
            base = (base + "\n\n" + kb_section).strip()

    # ── Project mode (when mounted in a Claude-style workspace) ──
    project_id = ctx.get("project_id")
    if project_id:
        proj_section = _build_project_section(
            project_name=ctx.get("project_name") or "",
            project_instructions=ctx.get("project_instructions") or "",
            folder_name=ctx.get("project_folder_name") or "",
            folder_kind=ctx.get("project_folder_kind") or "",
            project_files=ctx.get("project_files") or [],
        )
        if proj_section:
            base = (base + "\n\n" + proj_section).strip()

    # Store template in cache (with placeholder instead of real time)
    template = base.replace(now, "{now}") if now in base else base
    # Also replace the placeholder back to {now} for storage
    template = template.replace(_NOW_PLACEHOLDER, "{now}")

    with _prompt_cache_lock:
        _prompt_cache[cache_key] = (monotonic() + _PROMPT_CACHE_TTL, template)

    # Return with real time
    return template.replace("{now}", now)


def select_tools(
    config: PromptConfig,
    ctx: Dict[str, Any] | None,
    all_tools: Iterable[Any],
) -> List[Any]:
    """Select tools according to allowlist/routing config.

    Note: tool objects are expected to have a stable `.name` attribute.
    """

    allowed = set(config.tools.allowed or [])
    if not allowed:
        return list(all_tools)

    selected: List[Any] = []
    for tool in all_tools:
        name = getattr(tool, "name", None)
        # Support AgentScope JSON schemas (dict with function.name)
        if not name and isinstance(tool, dict):
            func_info = tool.get("function", {})
            name = func_info.get("name") if isinstance(func_info, dict) else None
        if not name:
            continue
        if name in allowed:
            selected.append(tool)

    # If allowlist accidentally filters everything, fail open.
    if not selected and not config.tools.routing.strict_allowlist:
        return list(all_tools)

    return selected
