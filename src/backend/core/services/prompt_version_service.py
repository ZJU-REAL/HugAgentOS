"""Prompt version pool service.

Stores multiple versions of system / code_exec / distillation prompts in
ContentBlock(id="prompt_versions") and provides activation + CRUD.

Design:
- Single ContentBlock row holds {active: {kind: version_id}, versions: [...]}.
- Each version has (kind, id) as its composite key.
- `get_active_version(kind)` returns the currently active version for a kind,
  or None if the DB is empty — callers then fall back to filesystem.
- Filesystem directories (v4/, code_exec/, distillation/) serve as the
  "seed" and as the last-resort fallback when DB is unreachable.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.content.content_blocks import (
    DEFAULT_PROMPT_VERSIONS,
    PROMPT_VERSIONS_BLOCK_ID,
)
from core.db.engine import SessionLocal
from core.db.models import ContentBlock

logger = logging.getLogger(__name__)


VALID_KINDS = ("system", "code_exec", "distillation", "plan_mode")

# Process-local cache for the payload, invalidated on write.
_payload_cache: Optional[Dict[str, Any]] = None
_payload_cache_lock = Lock()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _backend_root() -> Path:
    # src/backend
    return Path(__file__).resolve().parents[2]


def _fs_dir(kind: str) -> Path:
    root = _backend_root() / "prompts" / "prompt_text"
    if kind == "system":
        return root / "default" / "system"
    if kind == "code_exec":
        return root / "code_exec" / "system"
    if kind == "distillation":
        return root / "distillation"
    if kind == "plan_mode":
        return root / "plan_mode"
    raise ValueError(f"unknown kind: {kind}")


def _read_fs_parts(kind: str) -> List[Dict[str, Any]]:
    """Read on-disk markdown into a parts[] list.

    For system/code_exec: each *.system.md file under the kind's system/ dir
    becomes one part. part_id = "system/<name>" (name stripped of .system.md).
    For distillation / plan_mode: single *.system.md → one part.
    """
    parts: List[Dict[str, Any]] = []
    dirp = _fs_dir(kind)

    if kind == "distillation":
        # Each *.system.md is a separate part (skill_distiller is always first, for
        # backward compatibility).
        # Note: distillation's parts are mutually independent prompts (used individually
        # by part_id, see render_active_prompt_part), unlike system which is concatenated
        # into a single prompt.
        if not dirp.is_dir():
            return parts
        files = sorted(
            (f for f in os.listdir(dirp) if f.endswith(".system.md")),
            key=lambda f: (f != "skill_distiller.system.md", f),
        )
        for idx, fname in enumerate(files):
            name = fname[: -len(".system.md")]
            parts.append({
                "part_id": name,
                "display_name": name,
                "content": (dirp / fname).read_text(encoding="utf-8"),
                "sort_order": idx * 10,
                "is_enabled": True,
            })
        return parts

    if kind == "plan_mode":
        fp = dirp / "plan_mode.system.md"
        if fp.exists():
            parts.append({
                "part_id": "plan_mode",
                "display_name": "plan_mode",
                "content": fp.read_text(encoding="utf-8"),
                "sort_order": 0,
                "is_enabled": True,
            })
        return parts

    if not dirp.is_dir():
        return parts
    files = sorted(f for f in os.listdir(dirp) if f.endswith(".system.md"))
    for idx, fname in enumerate(files):
        name = fname[: -len(".system.md")]
        content = (dirp / fname).read_text(encoding="utf-8")
        part_id = f"system/{name}"
        parts.append({
            "part_id": part_id,
            "display_name": name,
            "content": content,
            "sort_order": idx * 10,
            "is_enabled": True,
        })
    return parts


def _default_version_for_kind(kind: str, version_id: Optional[str] = None) -> Dict[str, Any]:
    """Build a new version dict from filesystem for a given kind."""
    if kind == "system":
        vid = version_id or "default"
        name = "default - 标准系统提示词"
        desc = "当前默认系统提示词，包含 role / constraints / tools / workflow / format"
    elif kind == "code_exec":
        vid = version_id or "default"
        name = "default - 代码执行 (沙盒)"
        desc = "Lab 代码执行模式的系统提示词（沙盒环境、工具能力、执行规范等）"
    elif kind == "distillation":
        vid = version_id or "default"
        name = "default - 技能蒸馏"
        desc = "从对话轨迹蒸馏出可复用技能的系统提示词"
    elif kind == "plan_mode":
        vid = version_id or "default"
        name = "default - 计划模式"
        desc = "Plan 模式下用于拆解用户任务为可执行步骤的 sub-agent 系统提示词"
    else:
        raise ValueError(f"unknown kind: {kind}")

    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": vid,
        "kind": kind,
        "name": name,
        "description": desc,
        "parts": _read_fs_parts(kind),
        "created_at": now,
        "updated_at": now,
    }


# ── Payload load / save ─────────────────────────────────────────────────────

def _load_payload(db: Optional[Session] = None) -> Dict[str, Any]:
    """Read current prompt_versions payload from DB (cached)."""
    global _payload_cache
    with _payload_cache_lock:
        if _payload_cache is not None:
            # Return shallow copy so callers can't mutate the cache
            return _payload_cache

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        row = db.query(ContentBlock).filter(
            ContentBlock.id == PROMPT_VERSIONS_BLOCK_ID
        ).first()
        if row and isinstance(row.payload, dict):
            payload = row.payload
        else:
            payload = _clone(DEFAULT_PROMPT_VERSIONS)
    finally:
        if own_session:
            db.close()

    with _payload_cache_lock:
        _payload_cache = payload
    return payload


def _save_payload(
    payload: Dict[str, Any],
    *,
    db: Optional[Session] = None,
    updated_by: str = "system",
) -> None:
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        row = db.query(ContentBlock).filter(
            ContentBlock.id == PROMPT_VERSIONS_BLOCK_ID
        ).first()
        now = datetime.now(timezone.utc)
        if row:
            row.payload = payload
            row.updated_at = now
            row.updated_by = updated_by
        else:
            row = ContentBlock(
                id=PROMPT_VERSIONS_BLOCK_ID,
                payload=payload,
                updated_at=now,
                updated_by=updated_by,
            )
            db.add(row)
        db.commit()
    finally:
        if own_session:
            db.close()

    invalidate_cache()


def invalidate_cache() -> None:
    """Drop in-process payload cache. Called on writes + from prompt cache invalidators."""
    global _payload_cache
    with _payload_cache_lock:
        _payload_cache = None


def _clone(obj: Any) -> Any:
    import copy
    return copy.deepcopy(obj)


# ── Public API ──────────────────────────────────────────────────────────────

def list_versions(kind: Optional[str] = None, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    payload = _load_payload(db)
    versions = payload.get("versions") or []
    active = payload.get("active") or {}
    items = []
    for v in versions:
        if kind and v.get("kind") != kind:
            continue
        items.append({
            "id": v.get("id"),
            "kind": v.get("kind"),
            "name": v.get("name") or v.get("id"),
            "description": v.get("description") or "",
            "parts_count": len(v.get("parts") or []),
            "is_active": active.get(v.get("kind")) == v.get("id"),
            "created_at": v.get("created_at"),
            "updated_at": v.get("updated_at"),
        })
    return items


def get_version(kind: str, version_id: str, db: Optional[Session] = None) -> Optional[Dict[str, Any]]:
    if kind not in VALID_KINDS:
        return None
    payload = _load_payload(db)
    active = payload.get("active") or {}
    for v in payload.get("versions") or []:
        if v.get("kind") == kind and v.get("id") == version_id:
            data = _clone(v)
            data["is_active"] = active.get(kind) == version_id
            return data
    return None


def get_active_version(kind: str, db: Optional[Session] = None) -> Optional[Dict[str, Any]]:
    """Return the active version for a kind, or None if not found / DB empty."""
    if kind not in VALID_KINDS:
        return None
    payload = _load_payload(db)
    active_id = (payload.get("active") or {}).get(kind)
    if not active_id:
        return None
    for v in payload.get("versions") or []:
        if v.get("kind") == kind and v.get("id") == active_id:
            return _clone(v)
    return None


def upsert_version(
    kind: str,
    version_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    parts: Optional[List[Dict[str, Any]]] = None,
    from_id: Optional[str] = None,
    db: Optional[Session] = None,
    updated_by: str = "admin",
) -> Dict[str, Any]:
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    if not version_id:
        raise ValueError("version_id required")

    payload = _clone(_load_payload(db))
    versions: List[Dict[str, Any]] = payload.setdefault("versions", [])
    now = datetime.now(timezone.utc).isoformat()

    existing = next((v for v in versions if v.get("kind") == kind and v.get("id") == version_id), None)

    # If cloning, look up source (optional)
    source_parts: List[Dict[str, Any]] = []
    if from_id:
        src = next((v for v in versions if v.get("kind") == kind and v.get("id") == from_id), None)
        if src:
            source_parts = _clone(src.get("parts") or [])
            if name is None:
                name = f"{src.get('name') or from_id} 副本"
            if description is None:
                description = src.get("description") or ""

    if existing:
        if name is not None:
            existing["name"] = name
        if description is not None:
            existing["description"] = description
        if parts is not None:
            existing["parts"] = _normalize_parts(parts)
        existing["updated_at"] = now
        saved = existing
    else:
        # On create: if from_id is passed and parts is empty (empty list / not passed),
        # always treat it as a clone.
        # Only explicitly passing non-empty parts overrides the clone's source content.
        if parts and len(parts) > 0:
            final_parts = _normalize_parts(parts)
        elif from_id and source_parts:
            final_parts = source_parts
        else:
            final_parts = _normalize_parts(parts) if parts is not None else []
        saved = {
            "id": version_id,
            "kind": kind,
            "name": name or version_id,
            "description": description or "",
            "parts": final_parts,
            "created_at": now,
            "updated_at": now,
        }
        versions.append(saved)

    _save_payload(payload, db=db, updated_by=updated_by)
    return saved


def delete_version(kind: str, version_id: str, db: Optional[Session] = None) -> None:
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    payload = _clone(_load_payload(db))
    active = payload.get("active") or {}
    if active.get(kind) == version_id:
        raise ValueError("cannot delete the currently active version; activate another first")

    versions: List[Dict[str, Any]] = payload.get("versions") or []
    new_list = [v for v in versions if not (v.get("kind") == kind and v.get("id") == version_id)]
    if len(new_list) == len(versions):
        raise KeyError(f"version not found: {kind}/{version_id}")
    payload["versions"] = new_list
    _save_payload(payload, db=db)


def activate_version(kind: str, version_id: str, db: Optional[Session] = None) -> None:
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    payload = _clone(_load_payload(db))
    versions: List[Dict[str, Any]] = payload.get("versions") or []
    if not any(v.get("kind") == kind and v.get("id") == version_id for v in versions):
        raise KeyError(f"version not found: {kind}/{version_id}")
    payload.setdefault("active", {})[kind] = version_id
    _save_payload(payload, db=db)
    # Downstream prompt builders cache by (active_id, updated_at); poke their cache too.
    try:
        from prompts.prompt_runtime import invalidate_prompt_cache
        invalidate_prompt_cache()
    except Exception:
        pass


def _normalize_parts(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for idx, p in enumerate(parts or []):
        pid = (p.get("part_id") or "").strip()
        if not pid:
            continue
        result.append({
            "part_id": pid,
            "display_name": p.get("display_name") or pid.split("/")[-1],
            "content": p.get("content") or "",
            "sort_order": int(p.get("sort_order") if p.get("sort_order") is not None else idx * 10),
            "is_enabled": bool(p.get("is_enabled", True)),
        })
    # Stable sort by sort_order
    result.sort(key=lambda x: x["sort_order"])
    return result


# ── Seeding ─────────────────────────────────────────────────────────────────

def seed_from_filesystem(
    *,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """Create default versions from filesystem if not already present."""
    payload = _clone(_load_payload(db))
    versions: List[Dict[str, Any]] = payload.setdefault("versions", [])
    active: Dict[str, str] = payload.setdefault("active", {})
    changed = False

    def exists(kind: str, vid: str) -> bool:
        return any(v.get("kind") == kind and v.get("id") == vid for v in versions)

    added: List[str] = []

    # ── One-time migration: rename legacy system/v4 → system/default ──
    # Keeps the user's existing edits; the name change aligns the in-code
    # default version id with other kinds (code_exec/default, distillation/default, …).
    v4_row = next((v for v in versions if v.get("kind") == "system" and v.get("id") == "v4"), None)
    default_row = next((v for v in versions if v.get("kind") == "system" and v.get("id") == "default"), None)
    if v4_row and not default_row:
        v4_row["id"] = "default"
        # Refresh display name only if it still looks like the factory label
        if v4_row.get("name", "").startswith("v4 - 标准版"):
            v4_row["name"] = "default - 标准系统提示词"
        if active.get("system") == "v4":
            active["system"] = "default"
        added.append("system/v4 → system/default (renamed)")
        changed = True

    # ── One-time migration: extract system/90_plan_mode from every system
    # version into a new plan_mode/default version (if not already seeded).
    # After extraction, the system/90_plan_mode part is REMOVED from system
    # versions so the main agent prompt no longer duplicates it.
    if not exists("plan_mode", "default"):
        extracted_content: Optional[str] = None
        for v in versions:
            if v.get("kind") != "system":
                continue
            new_parts: List[Dict[str, Any]] = []
            for p in v.get("parts") or []:
                if (p.get("part_id") or "").strip() == "system/90_plan_mode":
                    if extracted_content is None and (p.get("content") or "").strip():
                        extracted_content = p["content"]
                    changed = True
                    continue
                new_parts.append(p)
            v["parts"] = new_parts
        if extracted_content:
            now = datetime.now(timezone.utc).isoformat()
            versions.append({
                "id": "default",
                "kind": "plan_mode",
                "name": "default - 计划模式（从 v4 迁移）",
                "description": "由历史 v4 system/90_plan_mode 片段迁移而来的 plan_mode 默认版本",
                "parts": [{
                    "part_id": "plan_mode",
                    "display_name": "plan_mode",
                    "content": extracted_content,
                    "sort_order": 0,
                    "is_enabled": True,
                }],
                "created_at": now,
                "updated_at": now,
            })
            active["plan_mode"] = "default"
            added.append("plan_mode/default (migrated)")
            changed = True

    # Default versions (v4 system + code_exec + distillation + plan_mode)
    for kind in VALID_KINDS:
        default_id = DEFAULT_PROMPT_VERSIONS["active"].get(kind)
        if default_id and not exists(kind, default_id):
            versions.append(_default_version_for_kind(kind, default_id))
            added.append(f"{kind}/{default_id}")
            changed = True
        if kind not in active:
            active[kind] = default_id
            changed = True

    # ── Backfill: distillation multi-part expansion ──
    # In older databases, distillation/default has only the single skill_distiller part;
    # add the newly-introduced independent prompt parts from the filesystem
    # (session_digest / colleague_distiller / personal_distiller) into that version,
    # leaving existing parts' content untouched, so they can be edited in Config's
    # prompt management.
    dist_default_id = DEFAULT_PROMPT_VERSIONS["active"].get("distillation") or "default"
    dist_default = next(
        (v for v in versions if v.get("kind") == "distillation" and v.get("id") == dist_default_id),
        None,
    )
    if dist_default is not None:
        existing_pids = {(p.get("part_id") or "").strip() for p in dist_default.get("parts") or []}
        for fs_part in _read_fs_parts("distillation"):
            if fs_part["part_id"] not in existing_pids:
                dist_default.setdefault("parts", []).append(fs_part)
                added.append(f"distillation/{dist_default_id}:{fs_part['part_id']}")
                changed = True

    if added or changed:
        _save_payload(payload, db=db, updated_by="system_seed")
        if added:
            logger.info("[prompt_version_service] seeded: %s", ", ".join(added))
    return {"added": added, "active": active}


# ── Assembled prompt helpers (used by runtime callers) ──────────────────────

def render_active_prompt(kind: str, db: Optional[Session] = None) -> Optional[str]:
    """Concatenate enabled parts of the active version into a single string.

    Returns None if no active version exists in DB (caller should fall back).
    """
    v = get_active_version(kind, db=db)
    if not v:
        return None
    parts = v.get("parts") or []
    chunks: List[str] = []
    for p in parts:
        if not p.get("is_enabled", True):
            continue
        content = (p.get("content") or "").strip()
        if content:
            chunks.append(content)
    return "\n\n".join(chunks) if chunks else None


def render_active_prompt_part(
    kind: str, part_id: str, db: Optional[Session] = None
) -> Optional[str]:
    """Return a single named part of the active version (independent prompts).

    Used by distillation-family prompts where each part is a standalone
    system prompt (skill_distiller / session_digest / colleague_distiller /
    personal_distiller) rather than a concatenation segment.

    Back-compat: an active version that predates the multi-part layout holds
    exactly one part (the legacy skill_distiller prompt) — only a request for
    ``skill_distiller`` may fall back to it; other part_ids must not silently
    receive the wrong prompt.
    Returns None if no active version / no match (caller falls back to fs).
    """
    v = get_active_version(kind, db=db)
    if not v:
        return None
    parts = v.get("parts") or []
    for p in parts:
        if p.get("part_id") == part_id:
            content = (p.get("content") or "").strip()
            return content or None
    if len(parts) == 1 and part_id == "skill_distiller":
        content = (parts[0].get("content") or "").strip()
        return content or None
    return None


def render_code_capability_segment(db: Optional[Session] = None) -> str:
    """The **single source of truth** for the code-execution capability (``code_exec``)
    prompt segment.

    When CODE_CAPABILITY_ENABLED is on in all modes (or in a Lab code_exec session), at
    runtime agent_factory appends this segment to the end of the system prompt. The
    Config backend ``/v1/admin/prompts/preview`` also calls this function, ensuring the
    main-agent prompt shown in the backend **does not drift** from what the agent
    actually sees (fixes "the backend was missing the code-execution segment").

    Priority: DB code_exec active version → filesystem fallback
    (``prompts/prompt_text/code_exec/system/*.system.md``). Returns "" if there is no
    content.
    """
    try:
        rendered = render_active_prompt("code_exec", db=db)
        if rendered:
            return rendered
    except Exception:
        logger.debug("render code_exec active prompt failed", exc_info=True)
    # Filesystem fallback: src/backend/prompts/prompt_text/code_exec/system/
    backend_root = os.path.dirname(
        os.path.dirname(os.path.dirname(__file__))
    )
    ce_dir = os.path.join(
        backend_root, "prompts", "prompt_text", "code_exec", "system"
    )
    if os.path.isdir(ce_dir):
        parts: List[str] = []
        for fn in sorted(
            f for f in os.listdir(ce_dir) if f.endswith(".system.md")
        ):
            try:
                with open(os.path.join(ce_dir, fn), "r", encoding="utf-8") as f:
                    parts.append(f.read())
            except Exception:
                continue
        if parts:
            return "\n\n".join(parts)
    return ""
