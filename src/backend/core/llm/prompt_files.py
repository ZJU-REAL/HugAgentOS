"""Read prompt parts from markdown files on disk."""
from __future__ import annotations
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from core.llm import prompt_kinds
from core.llm import prompt_storage
from core.llm import prompt_versions_state

def backend_root() -> Path:
    # src/backend
    return Path(__file__).resolve().parents[2]


def fs_dir(kind: str) -> Path:
    spec = prompt_kinds.KIND_SPECS.get(kind)
    if spec is None:
        raise ValueError(f"unknown kind: {kind}")
    if spec.root is not None:
        return spec.root
    return backend_root().joinpath("prompts", "prompt_text", *spec.subdir)


def fs_signature(dirp: Path) -> tuple:
    """目录里每个 md 的 (文件名, mtime, 大小)。用它判断要不要重读磁盘。"""
    try:
        names = sorted(
            [p.name for p in dirp.glob("*.system.md")]
            + [p.relative_to(dirp).as_posix() for p in dirp.glob("*/system.md")]
        )
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


def read_fs_parts(kind: str) -> List[Dict[str, Any]]:
    """Read on-disk markdown into a parts[] list.

    布局由 :data:`KIND_SPECS` 的 ``layout`` 决定（concat / independent / single）。
    concat 版的 sort_order 取文件名的数字前缀（``05_x`` → 5），新增文件落到它本该
    在的位置，不挤动相邻段。

    结果按目录内容签名缓存：这些 md 是只读的部署产物，而本函数落在每轮对话的
    提示词兜底路径和管理台的每次 GET 上，不做缓存就是反复读同一批文件。
    """
    dirp = fs_dir(kind)
    sig = fs_signature(dirp)
    with prompt_versions_state.process_state().fs_parts_cache_lock:
        cached = prompt_versions_state.process_state().fs_parts_cache.get(kind)
        if cached is not None and cached[0] == sig:
            return prompt_storage.clone(cached[1])

    parts = build_fs_parts(kind, dirp, [name for name, _, _ in sig])
    with prompt_versions_state.process_state().fs_parts_cache_lock:
        prompt_versions_state.process_state().fs_parts_cache[kind] = (sig, parts)
    return prompt_storage.clone(parts)


def prompt_part_id(filename: str) -> str:
    """Map a flat prompt file or an agent package prompt to its stable part ID."""
    if filename.endswith("/system.md"):
        return Path(filename).parent.name
    return filename.removesuffix(".system.md")


def build_fs_parts(kind: str, dirp: Path, files: List[str]) -> List[Dict[str, Any]]:
    layout = prompt_kinds.KIND_SPECS[kind].layout

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
                "part_id": prompt_part_id(fname),
                "display_name": prompt_part_id(fname),
                "content": (dirp / fname).read_text(encoding="utf-8"),
                "sort_order": idx * 10,
                "is_enabled": True,
            }
            for idx, fname in enumerate(files)
        ]

    parts: List[Dict[str, Any]] = []
    for idx, fname in enumerate(files):
        name = prompt_part_id(fname)
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


def default_version_for_kind(kind: str, version_id: Optional[str] = None) -> Dict[str, Any]:
    """Build a new version dict from filesystem for a given kind."""
    spec = prompt_kinds.KIND_SPECS.get(kind)
    if spec is None:
        raise ValueError(f"unknown kind: {kind}")

    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": version_id or prompt_versions_state.DEFAULT_VERSION_ID,
        "kind": kind,
        "name": spec.name,
        "description": spec.desc,
        "parts": read_fs_parts(kind),
        "created_at": now,
        "updated_at": now,
    }
