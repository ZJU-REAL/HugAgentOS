"""Prompt version pool service.

Stores multiple versions of system / code_exec / distillation / sub-agent prompts in
ContentBlock(id="prompt_versions") and provides activation + CRUD.

Design:
- Single ContentBlock row holds {active: {kind: version_id}, versions: [...]}.
- Each version has (kind, id) as its composite key.
- `get_active_version(kind)` returns the currently active version for a kind,
  or None if the DB is empty — callers then fall back to filesystem.
- Filesystem directories (default/, code_exec/, distillation/, subagents/) serve as the
  "seed" and as the last-resort fallback when DB is unreachable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from core.content.content_blocks import DEFAULT_PROMPT_VERSIONS, PROMPT_VERSIONS_BLOCK_ID
from core.db.engine import SessionLocal
from core.db.models import ContentBlock
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


#: 每个版本池默认版本的 id。内置 kind 与自定义 kind 共用这一个约定。
DEFAULT_VERSION_ID = "default"


@dataclass(frozen=True)
class KindSpec:
    """一个内置 kind 的全部差异都集中在这里，别处不再按 kind 分支。

    ``layout`` 决定目录里的 md 文件怎么变成 parts：

    - ``concat``：目录下每个 ``*.system.md`` 是一段，拼起来构成一份提示词
    - ``independent``：每个文件是一份互不相干的独立提示词，按 part_id 单取
    - ``single``：整个 kind 只有 ``<kind>.system.md`` 一个文件
    """

    subdir: tuple[str, ...]
    layout: str
    label: str
    name: str
    desc: str


#: 内置 kind：各自对应一段固定的运行时装配位置，有文件系统兜底，不可删。
#: 新增一个内置 kind = 这张表加一行 + 放好 md 文件，不必再改别处。
KIND_SPECS: Dict[str, KindSpec] = {
    "system": KindSpec(
        ("default", "system"),
        "concat",
        "系统提示词",
        "default - 标准系统提示词",
        "当前默认系统提示词，包含 role / constraints / tools / workflow / format",
    ),
    "turbo": KindSpec(
        ("turbo",),
        "single",
        "极速模式",
        "default - 极速模式",
        "极速模式（快速查询）系统提示词：仅联网搜索/网页抓取/知识库检索三类工具，"
        "1-2 轮并行调用后直接作答",
    ),
    "plan_mode": KindSpec(
        ("plan_mode",),
        "single",
        "计划模式",
        "default - 计划模式",
        "Plan 模式下用于拆解用户任务为可执行步骤的 sub-agent 系统提示词",
    ),
    "plan_tool": KindSpec(
        ("plan_tool",),
        "single",
        "任务计划清单",
        "default - 任务计划清单",
        "顶层对话注册 update_plan 工具时追加的计划清单说明",
    ),
    "code_exec": KindSpec(
        ("code_exec", "system"),
        "concat",
        "代码执行",
        "default - 代码执行 (沙盒)",
        "Lab 代码执行模式的系统提示词（沙盒环境、工具能力、执行规范等）",
    ),
    "distillation": KindSpec(
        ("distillation",),
        "independent",
        "蒸馏",
        "default - 技能蒸馏",
        "从对话轨迹蒸馏出可复用技能的系统提示词",
    ),
    "subagents": KindSpec(
        ("subagents",),
        "independent",
        "平台默认子智能体",
        "default - 平台默认子智能体",
        "探索员、执行员和审查员三个平台内置角色的独立系统提示词",
    ),
}

BUILTIN_KINDS = tuple(KIND_SPECS)

#: 兼容别名。历史上这个名字既是"内置清单"也是"合法性白名单"；自定义 kind 出现后
#: 两者分家了——校验一律走 :func:`is_valid_kind`，这里只保留内置那批。
VALID_KINDS = BUILTIN_KINDS


def list_custom_kinds(db: Optional[Session] = None) -> List[Dict[str, str]]:
    """管理员自建的 kind（``[{"key","label"}]``）。

    存在版本池同一个 ContentBlock 的 ``custom_kinds`` 下——它们和内置 kind 共用
    versions/active 两张表，差别只在于没有文件系统兜底、可以删。
    「对话模式」就是靠这个扩展的：管理员在提示词管理新开一个 tab，模式那边就能绑它。
    """
    payload = _load_payload(db)
    out: List[Dict[str, str]] = []
    for item in payload.get("custom_kinds") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key and key not in BUILTIN_KINDS:
            out.append({"key": key, "label": str(item.get("label") or key)})
    return out


def all_kinds(db: Optional[Session] = None) -> List[Dict[str, str]]:
    """内置 + 自定义的完整 kind 清单，供管理端渲染 tab 与模式绑定下拉。"""
    items = [{"key": k, "label": s.label, "builtin": True} for k, s in KIND_SPECS.items()]
    items += [{**c, "builtin": False} for c in list_custom_kinds(db)]
    return items


def is_valid_kind(kind: str, db: Optional[Session] = None) -> bool:
    if kind in BUILTIN_KINDS:
        return True
    return any(c["key"] == kind for c in list_custom_kinds(db))


def create_custom_kind(
    key: str, label: str, db: Optional[Session] = None, updated_by: str = "admin"
) -> Dict[str, str]:
    """新开一个 kind（tab），并给它建一个空的 ``default`` 版本并激活。

    没有初始版本的 kind 在管理端会是个点进去什么都没有的空 tab，所以这里顺手建上，
    管理员进去直接就能写正文。
    """
    cleaned = "".join(
        ch if (ch.isalnum() and ch.isascii()) or ch in "-_" else "_" for ch in (key or "").strip().lower()
    ).strip("-_")
    if not cleaned:
        raise ValueError("kind key required")
    if cleaned in BUILTIN_KINDS:
        raise ValueError(f"「{cleaned}」是内置 kind，不能重复创建")
    payload = _clone(_load_payload(db))
    customs = payload.setdefault("custom_kinds", [])
    if any(str((c or {}).get("key")) == cleaned for c in customs):
        raise ValueError(f"kind「{cleaned}」已存在")
    customs.append({"key": cleaned, "label": (label or cleaned).strip()[:60]})

    # 空的初始版本：一个可编辑的 part，内容留空由管理员填。
    versions = payload.setdefault("versions", [])
    now = datetime.now(timezone.utc).isoformat()
    versions.append(
        {
            "kind": cleaned,
            "id": "default",
            "name": (label or cleaned).strip()[:60],
            "description": "",
            "parts": [{"part_id": cleaned, "display_name": label or cleaned, "content": ""}],
            "created_at": now,
            "updated_at": now,
            "updated_by": updated_by,
        }
    )
    payload.setdefault("active", {})[cleaned] = "default"
    _save_payload(payload, db=db, updated_by=updated_by)
    return {"key": cleaned, "label": (label or cleaned).strip()[:60]}


def delete_custom_kind(key: str, db: Optional[Session] = None) -> bool:
    """删掉一个自定义 kind 及其全部版本。内置 kind 拒绝删除。

    注意：绑了这个 kind 的对话模式会退回默认提示词装配（``render_kind_segment(fs_fallback=False)``
    取不到就返回空串，装配侧按"没配提示词"处理），不会让对话起不来。
    """
    if key in BUILTIN_KINDS:
        raise ValueError("内置 kind 不可删除")
    payload = _clone(_load_payload(db))
    customs = payload.get("custom_kinds") or []
    remaining = [c for c in customs if str((c or {}).get("key")) != key]
    if len(remaining) == len(customs):
        return False
    payload["custom_kinds"] = remaining
    payload["versions"] = [v for v in (payload.get("versions") or []) if v.get("kind") != key]
    (payload.get("active") or {}).pop(key, None)
    _save_payload(payload, db=db)
    return True

# Process-local cache for the payload, invalidated on write.
_payload_cache: Optional[Dict[str, Any]] = None
_payload_cache_lock = Lock()

# kind -> (目录签名, parts)。签名变了才重读磁盘，见 _read_fs_parts。
_fs_parts_cache: Dict[str, tuple] = {}
_fs_parts_cache_lock = Lock()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _backend_root() -> Path:
    # src/backend
    return Path(__file__).resolve().parents[2]


def _fs_dir(kind: str) -> Path:
    spec = KIND_SPECS.get(kind)
    if spec is None:
        raise ValueError(f"unknown kind: {kind}")
    return _backend_root().joinpath("prompts", "prompt_text", *spec.subdir)


def _fs_signature(dirp: Path) -> tuple:
    """目录里每个 md 的 (文件名, mtime, 大小)。用它判断要不要重读磁盘。"""
    try:
        names = sorted(f for f in os.listdir(dirp) if f.endswith(".system.md"))
    except OSError:
        return ()
    sig: List[tuple] = []
    for name in names:
        try:
            st = os.stat(dirp / name)
        except OSError:
            continue
        sig.append((name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _read_fs_parts(kind: str) -> List[Dict[str, Any]]:
    """Read on-disk markdown into a parts[] list.

    布局由 :data:`KIND_SPECS` 的 ``layout`` 决定（concat / independent / single）。
    concat 版的 sort_order 取文件名的数字前缀（``05_x`` → 5），新增文件落到它本该
    在的位置，不挤动相邻段。

    结果按目录内容签名缓存：这些 md 是只读的部署产物，而本函数落在每轮对话的
    提示词兜底路径和管理台的每次 GET 上，不做缓存就是反复读同一批文件。
    """
    dirp = _fs_dir(kind)
    sig = _fs_signature(dirp)
    with _fs_parts_cache_lock:
        cached = _fs_parts_cache.get(kind)
        if cached is not None and cached[0] == sig:
            return _clone(cached[1])

    parts = _build_fs_parts(kind, dirp, [name for name, _, _ in sig])
    with _fs_parts_cache_lock:
        _fs_parts_cache[kind] = (sig, parts)
    return _clone(parts)


def _build_fs_parts(kind: str, dirp: Path, files: List[str]) -> List[Dict[str, Any]]:
    layout = KIND_SPECS[kind].layout

    if layout == "single":
        fname = f"{kind}.system.md"
        if fname not in files:
            return []
        return [
            {
                "part_id": kind,
                "display_name": kind,
                "content": (dirp / fname).read_text(encoding="utf-8"),
                "sort_order": 0,
                "is_enabled": True,
            }
        ]

    if layout == "independent":
        # 每个文件是一份独立提示词（按 part_id 单取，见 render_active_prompt_part），
        # 不像 concat 那样拼成一份。skill_distiller 固定排头，兼容旧数据。
        if kind == "distillation":
            files = sorted(files, key=lambda f: (f != "skill_distiller.system.md", f))
        return [
            {
                "part_id": fname[: -len(".system.md")],
                "display_name": fname[: -len(".system.md")],
                "content": (dirp / fname).read_text(encoding="utf-8"),
                "sort_order": idx * 10,
                "is_enabled": True,
            }
            for idx, fname in enumerate(files)
        ]

    parts: List[Dict[str, Any]] = []
    for idx, fname in enumerate(files):
        name = fname[: -len(".system.md")]
        prefix = name.split("_", 1)[0]
        parts.append(
            {
                "part_id": f"system/{name}",
                "display_name": name,
                "content": (dirp / fname).read_text(encoding="utf-8"),
                "sort_order": int(prefix) if prefix.isdigit() else idx * 10,
                "is_enabled": True,
            }
        )
    return parts


def _default_version_for_kind(kind: str, version_id: Optional[str] = None) -> Dict[str, Any]:
    """Build a new version dict from filesystem for a given kind."""
    spec = KIND_SPECS.get(kind)
    if spec is None:
        raise ValueError(f"unknown kind: {kind}")

    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": version_id or DEFAULT_VERSION_ID,
        "kind": kind,
        "name": spec.name,
        "description": spec.desc,
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
        row = db.query(ContentBlock).filter(ContentBlock.id == PROMPT_VERSIONS_BLOCK_ID).first()
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
        row = db.query(ContentBlock).filter(ContentBlock.id == PROMPT_VERSIONS_BLOCK_ID).first()
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
    if not is_valid_kind(kind, db):
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
    if not is_valid_kind(kind, db):
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
    if not is_valid_kind(kind, db):
        raise ValueError(f"invalid kind: {kind}")
    if not version_id:
        raise ValueError("version_id required")

    payload = _clone(_load_payload(db))
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
    if not is_valid_kind(kind, db):
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
    if not is_valid_kind(kind, db):
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
    for kind in BUILTIN_KINDS:
        if not exists(kind, DEFAULT_VERSION_ID):
            default_row = _default_version_for_kind(kind, DEFAULT_VERSION_ID)
            versions.append(default_row)
            added.append(f"{kind}/{DEFAULT_VERSION_ID}")
            changed = True
        else:
            default_row = next(
                v for v in versions if v.get("kind") == kind and v.get("id") == DEFAULT_VERSION_ID
            )
            existing_pids = {
                (p.get("part_id") or "").strip() for p in default_row.get("parts") or []
            }
            for fs_part in _read_fs_parts(kind):
                if fs_part["part_id"] not in existing_pids:
                    default_row.setdefault("parts", []).append(fs_part)
                    added.append(f"{kind}/{DEFAULT_VERSION_ID}:{fs_part['part_id']}")
                    changed = True
        if kind not in active:
            active[kind] = DEFAULT_VERSION_ID
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

    Used by distillation and platform-subagent prompts where each part is a standalone
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


def _skipped_parts(kind: str) -> set:
    try:
        from core.config.local_mode import local_mode_enabled

        if local_mode_enabled():
            from prompts.desktop_workspace import SKIP_PARTS
            return SKIP_PARTS.get(kind) or set()
    except Exception:
        logger.debug("local-mode check failed while assembling %s", kind, exc_info=True)
    return set()


def render_kind_segment(
    kind: str, db: Optional[Session] = None, *, fs_fallback: bool = True
) -> str:
    """某个 kind 装配好的正文：DB 激活版本 → 文件系统默认，都没有就返回空串。

    这是「一个 kind 的正文怎么取」的唯一实现——代码执行段、极速模式、任务计划清单
    以及 Config 管理台 ``/v1/admin/prompts/preview`` 的预览都走它，预览与智能体
    实际看到的因此不会漂移。

    文件系统这一步读的就是播种用的那批 md，所以尚未播种的部署渲染出来的，与它之后
    存进库里的是同一份。``fs_fallback=False`` 用于「对话模式」绑定的 kind：绑了个
    空 kind 就该退回默认装配，而不是套用别的 kind 的话术。
    """
    skip = _skipped_parts(kind)
    try:
        rendered = _render_active_parts(kind, db=db, skip=skip)
        if rendered:
            return rendered
    except Exception:
        logger.debug("render %s active prompt failed", kind, exc_info=True)
    if not fs_fallback or kind not in KIND_SPECS:
        return ""
    chunks = [
        (p.get("content") or "").strip()
        for p in _read_fs_parts(kind)
        if p.get("is_enabled", True)
        and (p.get("content") or "").strip()
        and (p.get("part_id") or "") not in skip
    ]
    return "\n\n".join(chunks)


def _render_active_parts(kind: str, *, db: Optional[Session], skip: set) -> Optional[str]:
    """激活版本的正文，按 ``skip`` 剔除若干段。``skip`` 为空时等同 render_active_prompt。"""
    if not skip:
        return render_active_prompt(kind, db=db)
    version = get_active_version(kind, db=db)
    if not version:
        return None
    chunks = [
        (p.get("content") or "").strip()
        for p in (version.get("parts") or [])
        if p.get("is_enabled", True)
        and (p.get("content") or "").strip()
        and (p.get("part_id") or "").strip() not in skip
    ]
    return "\n\n".join(chunks) or None


#: 极速模式在库和文件都取不到时的最后兜底——它替换掉整份系统提示词，返回空串会让
#: 智能体裸奔，所以这一段必须留在代码里。
_TURBO_LAST_RESORT = (
    "你是极速查询助手。收到问题后，最多发起 1-2 轮工具调用（同一轮内可并行"
    "调用多个检索工具），拿到结果立即用中文简洁作答，并标注信息来源。"
    "不要使用超出检索范围的工具，不要反复迭代。"
)


def render_turbo_system_prompt(db: Optional[Session] = None) -> str:
    """极速模式（快速查询）的整份系统提示词。

    极速模式不做常规装配：智能体只带检索类工具，默认提示词的工具/流程段都不适用。
    """
    return render_kind_segment("turbo", db=db) or _TURBO_LAST_RESORT
