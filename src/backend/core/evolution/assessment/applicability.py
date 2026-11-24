"""运行时 L3 适用性判断 + Replay 覆盖要求（P4）.

manifest 是技能的机器可读边界：``applies_when`` / ``excludes_when`` /
``dependencies`` 全部由 L3 证据编译而来。这里回答两个问题：

* **运行时**：当前这次请求，这个技能适不适用？——排除条件命中即不适用；
  声明了图谱依赖且依赖检查器可用时，依赖失效即不适用。检查是 fail-open 的：
  拿不到上下文（没有 task_type、图谱不可达）时不拦，因为误拦一个可用技能
  是能力损失，而技能被打开后还有正文的「不适用范围」兜底。
* **回放时**：候选的回放集覆不覆盖它声称的行为面？——只用成功案例回放，
  验证不了失败恢复；没有负样本，验证不了边界。覆盖不足的候选停在 draft，
  而不是带着单面证据进入 shadow。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MANIFEST_FILE = "evolution_manifest.json"


def load_manifest(extra_files: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The manifest from an ``AdminSkill.extra_files`` mapping, or ``None``."""
    raw = (extra_files or {}).get(MANIFEST_FILE)
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def manifest_permits(
    manifest: Optional[Dict[str, Any]],
    *,
    task_type: Optional[str] = None,
    relation_active: Optional[Callable[[str], Optional[bool]]] = None,
) -> Tuple[bool, str]:
    """Whether a skill's manifest permits use in the current context.

    ``relation_active(relation_id)`` is injected (usually a Neo4j lookup) and
    may return ``None`` for "could not tell", which never blocks — only a
    definite ``False`` does. No manifest means a hand-authored or pre-manifest
    skill: always permitted, this gate governs only what evolution wrote.
    """
    if not manifest:
        return True, "no_manifest"

    if task_type:
        for exclude in manifest.get("excludes_when") or []:
            excluded = str(exclude.get("task_type") or "")
            if excluded and excluded == task_type:
                return False, f"excluded_task_type:{task_type}"

        applies_types = [
            str(entry.get("task_type") or "")
            for entry in manifest.get("applies_when") or []
            if entry.get("task_type")
        ]
        if applies_types and task_type not in applies_types:
            return False, f"outside_declared_task_types:{task_type}"

    if relation_active is not None:
        for dependency in manifest.get("dependencies") or []:
            relation_id = str(dependency.get("relation_id") or "")
            if not relation_id:
                continue
            state = relation_active(relation_id)
            if state is False:
                # L3 说这个技能依赖某个实体关系；关系已失效 → 技能不适用。
                return False, f"dependency_inactive:{relation_id}"

    return True, "permitted"


# ── Replay coverage ──────────────────────────────────────────────────────────
