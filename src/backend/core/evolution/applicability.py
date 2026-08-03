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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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


def filter_by_manifest(
    skill_manifests: Dict[str, Optional[Dict[str, Any]]],
    *,
    task_type: Optional[str] = None,
    relation_active: Optional[Callable[[str], Optional[bool]]] = None,
) -> List[str]:
    """Of ``{skill_id: manifest}``, the ids permitted in this context."""
    permitted: List[str] = []
    for skill_id, manifest in skill_manifests.items():
        ok, reason = manifest_permits(
            manifest, task_type=task_type, relation_active=relation_active
        )
        if ok:
            permitted.append(skill_id)
        else:
            logger.info("[applicability] %s withheld: %s", skill_id, reason)
    return permitted


# ── Replay coverage ──────────────────────────────────────────────────────────


def assess_replay_coverage(
    pack: Any,
    task_ids: Sequence[str],
) -> Dict[str, Any]:
    """Whether a replay set covers what the pack claims. ``pack`` is a
    :class:`~core.evolution.evidence_contract.CapabilityEvidencePackV2`.

    Required, each only when the pack actually contains that class of evidence:

    * ≥1 成功 Episode（有效性的最低门槛）；
    * pack 带失败恢复 → 回放集必须含失败 Episode；
    * pack 带负样本 → 回放集必须含负样本 Episode；
    * pack 的 L3 证据带依赖 → 回放集须同时覆盖「用到图谱」与「没用图谱」
      两类场景（当证据里两类都存在时）。
    """
    chosen = {str(t) for t in task_ids}
    missing: List[str] = []

    episodes = list(getattr(pack, "episodes", []) or [])
    by_id = {str(e.get("episode_id") or ""): e for e in episodes}
    selected = [by_id[t] for t in chosen if t in by_id]

    if not any(e.get("verdict") == "success" for e in selected):
        missing.append("success_episode")

    failure_ids = {
        str(f.get("episode_id") or "")
        for f in getattr(pack, "failure_recoveries", []) or []
    } - {""}
    if failure_ids and not (failure_ids & chosen):
        missing.append("failure_recovery_episode")

    negative_ids = {
        str(n.get("episode_id") or "")
        for n in getattr(pack, "negative_examples", []) or []
    } - {""}
    if negative_ids and not (negative_ids & chosen):
        missing.append("negative_example_episode")

    has_dependency = any(
        str(r.get("role") or "") in ("dependency", "constraint")
        for r in getattr(pack, "graph_context", []) or []
    )
    if has_dependency:
        with_graph = {e["episode_id"] for e in episodes if e.get("used_graph")}
        without_graph = {e["episode_id"] for e in episodes if not e.get("used_graph")}
        if with_graph and not (with_graph & chosen):
            missing.append("graph_dependency_episode")
        if without_graph and not (without_graph & chosen):
            missing.append("no_graph_dependency_episode")

    return {"covered": not missing, "missing": missing}
