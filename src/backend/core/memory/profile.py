"""L1 Profile Memory — bounded markdown profile memory.

Each (user_id, workspace_id) maps to one markdown profile, capped at 1500
characters (configurable). Read once at session start and injected into the
frozen block of the system prompt; unchanged for the whole session.

- `get()` / `get_sync()`: read (called at session start)
- `patch()`: incrementally append a fact; triggers compaction when over the char cap
- `compact()`: use a low-temperature LLM to compress the current profile under the char cap
- `delete()`: one-click forget
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from core.config.settings import settings
from core.memory.audit import record as audit_record
from core.memory.context import MemoryContext
from core.memory.sanitizer import sanitize

logger = logging.getLogger(__name__)

# Matches `**key**: value` or `- **key**: value` (colon may be : or ：)
_FIELD_LINE_RE = re.compile(r"^\s*-?\s*\*\*([^*]+?)\*\*\s*[:：]\s*(.+?)\s*$")


def _parse_profile(md: str) -> list[dict]:
    """Split the profile markdown into structured entries.

    Each line is recognized as:
    - `**key**: value`  → {"key": key, "value": value} (field line, upsertable)
    - anything else     → {"key": None, "raw": <original line>} (kept verbatim, not upsertable)
    """
    entries: list[dict] = []
    for raw in (md or "").splitlines():
        m = _FIELD_LINE_RE.match(raw)
        if m:
            entries.append({"key": m.group(1).strip(), "value": m.group(2).strip()})
        elif raw.strip():
            entries.append({"key": None, "raw": raw})
    return entries


def _serialize_profile(entries: list[dict]) -> str:
    lines: list[str] = []
    for e in entries:
        if e.get("key"):
            lines.append(f"- **{e['key']}**: {e['value']}")
        elif e.get("raw"):
            lines.append(e["raw"])
    return "\n".join(lines)


# ─── Read ──────────────────────────────────────────────────────────────────


async def get(user_id: str, workspace_id: str = "default") -> str:
    """Async read of the L1 profile. DB read takes roughly <20ms; safe to await directly at session start."""
    if not user_id:
        return ""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, get_sync, user_id, workspace_id)
    except Exception as exc:
        logger.warning("[profile_memory] async get failed user=%s ws=%s: %s", user_id, workspace_id, exc)
        return ""


def get_sync(user_id: str, workspace_id: str = "default") -> str:
    """Synchronous read (only use when confirmed not in an async context)."""
    if not user_id:
        return ""
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ProfileMemory

        with SessionLocal() as session:
            row = (
                session.query(ProfileMemory)
                .filter_by(user_id=user_id, workspace_id=workspace_id)
                .first()
            )
            return row.content_md if row else ""
    except Exception as exc:
        logger.warning("[profile_memory] get_sync failed user=%s ws=%s: %s", user_id, workspace_id, exc)
        return ""


# ─── Write (incremental) ───────────────────────────────────────────────────


async def upsert_field(
    ctx: MemoryContext,
    key: str,
    value: str,
    reason: Optional[str] = None,
) -> bool:
    """Idempotently update one field: replace the value if the key exists, otherwise append.

    `key` should preferably be namespaced: `identity.name` / `identity.dept` / `preference.verbosity`.
    Same key with the same value short-circuits (no profile change, no audit row).
    """
    return await upsert_fields(ctx, [(key, value, reason)])


async def upsert_fields(
    ctx: MemoryContext,
    fields: list[tuple[str, str, Optional[str]]],
) -> bool:
    """Upsert multiple fields at once (single transaction, single executor dispatch).

    Each element of `fields` is `(key, value, reason)`. Every value goes through
    sanitize independently; a field that hits sensitive words is rejected and
    audited on its own, while the other fields are still written.
    """
    if not ctx.user_id or not fields:
        return False

    # Sanitize first (synchronous, no DB access)
    prepared: list[dict] = []
    for key, value, reason in fields:
        if not key or not value:
            continue
        san = sanitize(value)
        if san.reject:
            await audit_record(
                ctx, action="write_rejected", layer="L1",
                reason=f"sanitizer hits: {','.join(san.hits)}",
            )
            continue
        prepared.append({"key": key, "value": san.text, "reason": reason, "hits": san.hits})

    if not prepared:
        return False

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _upsert_fields_sync, ctx, prepared, loop)
    except Exception as exc:
        logger.warning("[profile_memory] upsert_fields failed user=%s n=%d: %s",
                       ctx.user_id, len(prepared), exc)
        return False


def _schedule_compact(
    ctx: MemoryContext,
    loop: Optional[asyncio.AbstractEventLoop],
) -> None:
    """Dispatch compact() from the executor worker thread back onto the event loop.

    `_upsert_fields_sync` / `_patch_sync` run in a worker thread via
    `run_in_executor`, where **no event loop is running** — calling
    `asyncio.create_task()` directly raises `RuntimeError`, so compaction would
    never trigger when the profile exceeds `profile_max_chars`.
    `run_coroutine_threadsafe` is the correct primitive for handing a coroutine
    to a target loop across threads; fire-and-forget, result not awaited.
    """
    if loop is None:
        return
    coro = compact(ctx)
    try:
        asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError as exc:
        # Edge cases like the loop already being closed — skip compaction, main flow unaffected
        coro.close()
        logger.debug("[profile_memory] compact schedule skipped: %s", exc)


def _upsert_fields_sync(
    ctx: MemoryContext,
    prepared: list[dict],
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> bool:
    """Apply all field updates in a single transaction, then write audit rows in batch."""
    from core.db.engine import SessionLocal
    from core.db.models import ProfileMemory

    max_chars = settings.memory.profile_max_chars
    applied: list[dict] = []  # (key, value, reason, hits, action)

    with SessionLocal() as session:
        row = (
            session.query(ProfileMemory)
            .filter_by(user_id=ctx.user_id, workspace_id=ctx.workspace_id)
            .first()
        )
        entries = _parse_profile(row.content_md if row else "")
        index = {e["key"]: e for e in entries if e.get("key")}

        for item in prepared:
            key, value = item["key"], item["value"]
            existing = index.get(key)
            if existing is None:
                new_entry = {"key": key, "value": value}
                entries.append(new_entry)
                index[key] = new_entry
                applied.append({**item, "action": "write"})
            elif existing["value"] != value:
                existing["value"] = value
                applied.append({**item, "action": "update"})
            # else: value unchanged → skip (no audit row, no DB churn)

        if not applied:
            return True  # all no-ops

        new_md = _serialize_profile(entries)
        if row is None:
            row = ProfileMemory(
                user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                content_md=new_md,
                updated_at=datetime.now(timezone.utc),
            )
            session.add(row)
        else:
            row.content_md = new_md
            row.updated_at = datetime.now(timezone.utc)
        over_limit = len(new_md) > max_chars
        session.commit()

    for item in applied:
        _audit_sync_safe(ctx, item["action"], item["hits"], item["reason"], item["value"])

    if over_limit:
        _schedule_compact(ctx, loop)
    return True


async def patch(
    ctx: MemoryContext,
    patch_text: str,
    reason: Optional[str] = None,
) -> bool:
    """(Kept, rarely used) Append free text to the end of the profile; **prefer `upsert_field`**.

    If the profile exceeds `MEMORY_PROFILE_MAX_CHARS` after appending, compact() is called automatically.
    """
    if not ctx.user_id or not patch_text:
        return False

    result = sanitize(patch_text)
    if result.reject:
        await audit_record(
            ctx, action="write_rejected", layer="L1",
            reason=f"sanitizer hits: {','.join(result.hits)}",
        )
        return False

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _patch_sync, ctx, result.text, reason, result.hits, loop,
        )
    except Exception as exc:
        logger.warning("[profile_memory] patch failed user=%s: %s", ctx.user_id, exc)
        return False


def _patch_sync(
    ctx: MemoryContext,
    clean_text: str,
    reason: Optional[str],
    sanitizer_hits: list[str],
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> bool:
    from core.db.engine import SessionLocal
    from core.db.models import ProfileMemory

    max_chars = settings.memory.profile_max_chars

    with SessionLocal() as session:
        row = (
            session.query(ProfileMemory)
            .filter_by(user_id=ctx.user_id, workspace_id=ctx.workspace_id)
            .first()
        )
        if row is None:
            row = ProfileMemory(
                user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                content_md=clean_text,
                updated_at=datetime.now(timezone.utc),
            )
            session.add(row)
        else:
            existing = row.content_md or ""
            merged = f"{existing}\n- {clean_text}".strip() if existing else f"- {clean_text}"
            row.content_md = merged
            row.updated_at = datetime.now(timezone.utc)
        over_limit = len(row.content_md) > max_chars
        session.commit()

    # Audit (sync context, no await)
    _audit_sync_safe(ctx, "write", sanitizer_hits, reason, clean_text)

    if over_limit:
        # Trigger compaction asynchronously (does not block the caller)
        _schedule_compact(ctx, loop)

    return True


def _audit_sync_safe(ctx, action, hits, reason, content) -> None:
    """Sync-context audit wrapper; delegates to `audit.record_sync` (handles the enable switch + exceptions internally)."""
    from core.memory.audit import record_sync

    record_sync(
        ctx, action, "L1",
        content=content,
        reason=reason or (f"sanitizer: {','.join(hits)}" if hits else None),
    )


# ─── Compaction ────────────────────────────────────────────────────────────


COMPACT_PROMPT = """你收到一份用户档案 markdown，超过字符上限。请压缩到 {max_chars} 字符以内，保留事实密度最高的信息，丢弃重复与过时内容。

【保留优先级（从高到低）】
1. 身份（姓名、单位、部门、岗位）
2. 稳定偏好（输出格式、语言风格、禁忌）
3. 长期关注的业务领域（前 3 项）

【丢弃】
- 同类事实的重复表述，保留最新/最具体的一条
- 明确的一次性任务痕迹（"上次帮我做 Q3 分析"）

【输出】
直接返回压缩后的 markdown 文本，不要任何额外解释，不要加代码块标记。

原档案：
{content}
"""


async def compact(ctx: MemoryContext) -> bool:
    """Compress the profile under the char cap with a low-temperature LLM. On failure, falls back silently without modifying the original profile."""
    if not settings.memory.enabled:
        return False

    current = await get(ctx.user_id, ctx.workspace_id)
    max_chars = settings.memory.profile_max_chars
    if not current or len(current) <= max_chars:
        return False

    try:
        compressed = await _run_compact_llm(current, max_chars)
    except Exception as exc:
        logger.warning("[profile_memory] compact LLM call failed: %s", exc)
        return False

    if not compressed or len(compressed) > max_chars * 1.1:
        logger.warning("[profile_memory] compact result invalid (len=%d > %d*1.1)",
                       len(compressed) if compressed else 0, max_chars)
        return False

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_compacted_sync, ctx, compressed)
    except Exception as exc:
        logger.warning("[profile_memory] compact write failed: %s", exc)
        return False

    await audit_record(ctx, action="update", layer="L1", reason="compacted", content=compressed)
    return True


def _write_compacted_sync(ctx: MemoryContext, compressed: str) -> None:
    from core.db.engine import SessionLocal
    from core.db.models import ProfileMemory

    with SessionLocal() as session:
        row = (
            session.query(ProfileMemory)
            .filter_by(user_id=ctx.user_id, workspace_id=ctx.workspace_id)
            .first()
        )
        if row:
            row.content_md = compressed
            row.last_compacted_at = datetime.now(timezone.utc)
            row.updated_at = datetime.now(timezone.utc)
            session.commit()


async def _run_compact_llm(content: str, max_chars: int) -> str:
    """Call the dedicated memory LLM client for compaction (temperature 0.1, same as the mem0 default).

    Prefers the DB `memory` role config; env vars are the fallback.
    """
    from core.memory.extractors._base import _resolve_memory_model_config
    from openai import AsyncOpenAI

    base_url, api_key, model_name = _resolve_memory_model_config()
    if not base_url or not api_key or not model_name:
        raise RuntimeError("memory LLM config incomplete for compact")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0)
    prompt = COMPACT_PROMPT.format(max_chars=max_chars, content=content)
    resp = await client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=min(max_chars, 2000),
    )
    return (resp.choices[0].message.content or "").strip()


# ─── Delete (right to be forgotten) ────────────────────────────────────────


async def delete(ctx: MemoryContext) -> bool:
    """Delete the profile for the given user+workspace and write one forget audit row."""
    try:
        loop = asyncio.get_running_loop()
        deleted = await loop.run_in_executor(None, _delete_sync, ctx.user_id, ctx.workspace_id)
    except Exception as exc:
        logger.warning("[profile_memory] delete failed user=%s: %s", ctx.user_id, exc)
        return False

    if deleted:
        await audit_record(ctx, action="forget", layer="L1", reason="user requested forget")
    return deleted


def _delete_sync(user_id: str, workspace_id: str) -> bool:
    from core.db.engine import SessionLocal
    from core.db.models import ProfileMemory

    with SessionLocal() as session:
        row = session.query(ProfileMemory).filter_by(
            user_id=user_id, workspace_id=workspace_id,
        ).first()
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
