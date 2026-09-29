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

from core.llm.execution_manifest import PromptManifestBuilder, stable_hash
from prompts.prompt_config import PromptConfig
from prompts.provider import (
    FilesystemPromptProvider,
    InlinePromptProvider,
    hardcoded_minimal_system_prompt,
)

# ── System prompt TTL cache ──────────────────────────────────────────────
_PROMPT_CACHE_TTL = 300.0  # seconds
_PROMPT_NOW_SENTINEL = "__PROMPT_NOW_PLACEHOLDER__"
_prompt_cache_lock = Lock()
# key -> (expires_at, prompt_template_without_now, ordered section templates)
# Section templates stay in this process-local cache only; the persisted
# execution manifest receives hashes/references, never this plaintext.
_prompt_cache: Dict[tuple, Tuple[float, str, Tuple[Dict[str, Any], ...]]] = {}


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

from prompts.desktop_workspace import build_local_mode_guidance, build_environment_context, desktop_prompt_text


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
    for tool in tools or []:
        name = getattr(tool, "name", None)
        if not name and isinstance(tool, dict):
            func_info = tool.get("function", {})
            name = func_info.get("name") if isinstance(func_info, dict) else None
        if name:
            names.append(name)
    return tuple(sorted(names))


def _prompt_cache_context_hash(ctx: Dict[str, Any]) -> str:
    """Hash every complete dynamic input that may affect rendered prompt text.

    ``now`` is deliberately excluded because the cached template carries a
    placeholder. Tool objects are projected onto stable public definition
    fields rather than ``repr`` (which may contain a process-specific address).
    """

    payload: Dict[str, Any] = {}
    for key, value in ctx.items():
        if key == "now":
            continue
        if key == "tools":
            tools: List[Any] = []
            for tool in value or []:
                if isinstance(tool, dict):
                    tools.append(tool)
                    continue
                tools.append(
                    {
                        "name": getattr(tool, "name", ""),
                        "description": getattr(tool, "description", ""),
                        "parameters": getattr(tool, "parameters", None)
                        or getattr(tool, "input_schema", None)
                        or {},
                    }
                )
            payload[key] = tools
        else:
            payload[key] = value
    return stable_hash(payload)


# Project-mode section moved to prompts.project_section; re-export for compat
from prompts.project_section import (  # noqa: E402
    _format_size,
    PROJECT_FILE_LIST_CAP,
    PROJECT_MODE_PART_ID,
    PROJECT_MODE_DISPLAY_NAME,
    _PROJECT_MODE_DEFAULT_TEMPLATE,
    _render_file_list_block,
    _render_folder_scope_block,
    _render_instructions_block,
    _collapse_blanks,
    _get_project_mode_template,
    _build_project_section,
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
        parts.append(
            {
                "part_id": PROJECT_MODE_PART_ID,
                "content": _PROJECT_MODE_DEFAULT_TEMPLATE,
                "display_name": PROJECT_MODE_DISPLAY_NAME,
                # Placed after all existing parts; stands alone as a "dynamic appendix section" in the UI list
                "sort_order": max(max_order + 100, 9000),
                "is_enabled": True,
            }
        )
        pvs.upsert_version(
            "system",
            active["id"],
            name=active.get("name"),
            description=active.get("description"),
            parts=parts,
        )
        log.info(
            "[prompt_seed] seeded %s into active system version=%s",
            PROJECT_MODE_PART_ID,
            active["id"],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[prompt_seed] ensure_project_mode_part_seeded skipped: %s", exc)


from prompts.system_builder import build_system_prompt  # noqa: E402


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
