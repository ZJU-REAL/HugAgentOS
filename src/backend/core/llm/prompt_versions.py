"""Create, activate and seed prompt versions."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
import logging
from core.llm import prompt_files
from core.llm import prompt_kinds
from core.llm import prompt_storage
from core.llm import prompt_versions_state

logger = logging.getLogger(__name__)

def list_versions(kind: Optional[str] = None, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    payload = prompt_storage.load_payload(db)
    versions = payload.get("versions") or []
    active = payload.get("active") or {}
    items = []
    for v in versions:
        if kind and v.get("kind") != kind:
            continue
        items.append(
            {
                "id": v.get("id"),
                "kind": v.get("kind"),
                "name": v.get("name") or v.get("id"),
                "description": v.get("description") or "",
                "parts_count": len(v.get("parts") or []),
                "is_active": active.get(v.get("kind")) == v.get("id"),
                "created_at": v.get("created_at"),
                "updated_at": v.get("updated_at"),
            }
        )
    return items


def get_version(
    kind: str, version_id: str, db: Optional[Session] = None
) -> Optional[Dict[str, Any]]:
    if not prompt_kinds.is_valid_kind(kind, db):
        return None
    payload = prompt_storage.load_payload(db)
    active = payload.get("active") or {}
    for v in payload.get("versions") or []:
        if v.get("kind") == kind and v.get("id") == version_id:
            data = prompt_storage.clone(v)
            data["is_active"] = active.get(kind) == version_id
            return data
    return None


def get_active_version(kind: str, db: Optional[Session] = None) -> Optional[Dict[str, Any]]:
    """Return the active version for a kind, or None if not found / DB empty."""
    if not prompt_kinds.is_valid_kind(kind, db):
        return None
    payload = prompt_storage.load_payload(db)
    active_id = (payload.get("active") or {}).get(kind)
    if not active_id:
        return None
    for v in payload.get("versions") or []:
        if v.get("kind") == kind and v.get("id") == active_id:
            return prompt_storage.clone(v)
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
    if not prompt_kinds.is_valid_kind(kind, db):
        raise ValueError(f"invalid kind: {kind}")
    if not version_id:
        raise ValueError("version_id required")

    payload = prompt_storage.clone(prompt_storage.load_payload(db))
    versions: List[Dict[str, Any]] = payload.setdefault("versions", [])
    now = datetime.now(timezone.utc).isoformat()

    existing = next(
        (v for v in versions if v.get("kind") == kind and v.get("id") == version_id), None
    )

    # If cloning, look up source (optional)
    source_parts: List[Dict[str, Any]] = []
    if from_id:
        src = next((v for v in versions if v.get("kind") == kind and v.get("id") == from_id), None)
        if src:
            source_parts = prompt_storage.clone(src.get("parts") or [])
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
            existing["parts"] = normalize_parts(parts)
        existing["updated_at"] = now
        saved = existing
    else:
        # On create: if from_id is passed and parts is empty (empty list / not passed),
        # always treat it as a clone.
        # Only explicitly passing non-empty parts overrides the clone's source content.
        if parts and len(parts) > 0:
            final_parts = normalize_parts(parts)
        elif from_id and source_parts:
            final_parts = source_parts
        else:
            final_parts = normalize_parts(parts) if parts is not None else []
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

    prompt_storage.save_payload(payload, db=db, updated_by=updated_by)
    return saved


def delete_version(kind: str, version_id: str, db: Optional[Session] = None) -> None:
    if not prompt_kinds.is_valid_kind(kind, db):
        raise ValueError(f"invalid kind: {kind}")
    payload = prompt_storage.clone(prompt_storage.load_payload(db))
    active = payload.get("active") or {}
    if active.get(kind) == version_id:
        raise ValueError("cannot delete the currently active version; activate another first")

    versions: List[Dict[str, Any]] = payload.get("versions") or []
    new_list = [v for v in versions if not (v.get("kind") == kind and v.get("id") == version_id)]
    if len(new_list) == len(versions):
        raise KeyError(f"version not found: {kind}/{version_id}")
    payload["versions"] = new_list
    prompt_storage.save_payload(payload, db=db)


def activate_version(kind: str, version_id: str, db: Optional[Session] = None) -> None:
    if not prompt_kinds.is_valid_kind(kind, db):
        raise ValueError(f"invalid kind: {kind}")
    payload = prompt_storage.clone(prompt_storage.load_payload(db))
    versions: List[Dict[str, Any]] = payload.get("versions") or []
    if not any(v.get("kind") == kind and v.get("id") == version_id for v in versions):
        raise KeyError(f"version not found: {kind}/{version_id}")
    payload.setdefault("active", {})[kind] = version_id
    prompt_storage.save_payload(payload, db=db)
    # Downstream prompt builders cache by (active_id, updated_at); poke their cache too.
    try:
        from prompts.prompt_runtime import invalidate_prompt_cache

        invalidate_prompt_cache()
    except Exception as fallback_error:
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.llm.prompt_versions:activate_version', fallback_error)


def normalize_parts(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for idx, p in enumerate(parts or []):
        pid = (p.get("part_id") or "").strip()
        if not pid:
            continue
        result.append(
            {
                "part_id": pid,
                "display_name": p.get("display_name") or pid.split("/")[-1],
                "content": p.get("content") or "",
                "sort_order": int(
                    p.get("sort_order") if p.get("sort_order") is not None else idx * 10
                ),
                "is_enabled": bool(p.get("is_enabled", True)),
            }
        )
    # Stable sort by sort_order
    result.sort(key=lambda x: x["sort_order"])
    return result


def seed_from_filesystem(
    *,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """Create default versions from filesystem if not already present."""
    payload = prompt_storage.clone(prompt_storage.load_payload(db))
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
    default_row = next(
        (v for v in versions if v.get("kind") == "system" and v.get("id") == "default"), None
    )
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
            versions.append(
                {
                    "id": "default",
                    "kind": "plan_mode",
                    "name": "default - 计划模式（从 v4 迁移）",
                    "description": "由历史 v4 system/90_plan_mode 片段迁移而来的 plan_mode 默认版本",
                    "parts": [
                        {
                            "part_id": "plan_mode",
                            "display_name": "plan_mode",
                            "content": extracted_content,
                            "sort_order": 0,
                            "is_enabled": True,
                        }
                    ],
                    "created_at": now,
                    "updated_at": now,
                }
            )
            active["plan_mode"] = "default"
            added.append("plan_mode/default (migrated)")
            changed = True

    # 每个内置 kind 的默认版本：没有就按文件系统建；已有就把**新增**的 md 文件补进去。
    # 补这一步是因为默认版本只在首次冷启动时从文件系统建一次，之后新加的 *.system.md
    # 再也进不了已建库的部署。已有 part 的正文原样保留，管理员改过的不会被覆盖。
    for kind in list(prompt_kinds.KIND_SPECS):
        if not exists(kind, prompt_versions_state.DEFAULT_VERSION_ID):
            default_row = prompt_files.default_version_for_kind(kind, prompt_versions_state.DEFAULT_VERSION_ID)
            versions.append(default_row)
            added.append(f"{kind}/{prompt_versions_state.DEFAULT_VERSION_ID}")
            changed = True
        else:
            default_row = next(
                v for v in versions if v.get("kind") == kind and v.get("id") == prompt_versions_state.DEFAULT_VERSION_ID
            )
            existing_pids = {
                (p.get("part_id") or "").strip() for p in default_row.get("parts") or []
            }
            for fs_part in prompt_files.read_fs_parts(kind):
                if fs_part["part_id"] not in existing_pids:
                    default_row.setdefault("parts", []).append(fs_part)
                    added.append(f"{kind}/{prompt_versions_state.DEFAULT_VERSION_ID}:{fs_part['part_id']}")
                    changed = True
        if kind not in active:
            active[kind] = prompt_versions_state.DEFAULT_VERSION_ID
            changed = True

    if added or changed:
        prompt_storage.save_payload(payload, db=db, updated_by="system_seed")
        if added:
            logger.info("[prompt_version_service] seeded: %s", ", ".join(added))
    return {"added": added, "active": active}
