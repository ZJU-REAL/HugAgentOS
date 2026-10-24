"""L3 图谱演进 — 图谱不演进「怎么做」，演进结构与可信度（P5）.

L2 的演进对象是流程性知识；L3 的演进对象是语义图谱本身：

* 反复被检索、反复被观察的关系 → **强化**（weight 上调）；
* 长期无人再观察的关系 → **降权**——但绝不删除，陈旧不等于错误；
* 同一主体 + 功能型谓词出现不同 target → **冲突标记**，交人裁决；
* 更新的关系明显取代旧关系 → **替代建议**，交人裁决；
* Episode 反复依赖某实体而图谱没有对应边 → **缺边建议**，交人裁决；
* alias_of 边存在 → **别名合并建议**，交人裁决。

自动化边界（每一条都有明确理由）：

* **自动执行**：同作用域内的强化与陈旧降权。两者都可逆、都不改变图谱语义。
* **人工候选**：冲突 / 替代 / 合并 / 失效 / 缺边。它们改变图谱的**含义**，
  错一次的代价是下游所有依赖该关系的技能边界一起错。
* **永不因单次失败删除关系**：最强的自动操作是降权。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

OP_REINFORCE = "reinforce_relation"
OP_CREATE = "create_relation"
OP_MERGE_ALIAS = "merge_alias"
OP_REWEIGHT = "reweight_relation"
OP_SUPERSEDE = "supersede_relation"
OP_DEACTIVATE = "deactivate_relation"
OP_FLAG_CONTRADICTION = "flag_contradiction"
OP_SUGGEST_MISSING = "suggest_missing_relation"

# 只有这两类可以自动执行；其余全部生成人工候选。
AUTO_OPS = frozenset({OP_REINFORCE, OP_REWEIGHT})

# 谓词的「功能型」子集：同一主体在这些谓词上通常只有一个真值，
# 出现两个 target 即为冲突信号（而不是多值事实）。
FUNCTIONAL_PREDICATES = frozenset(
    {"depends_on", "belongs_to", "responsible_for", "located_in"}
)

# 超过这个天数没有再观察到 → 陈旧，降权一档。
STALE_DAYS = 90
# 每次降权乘的系数与地板：地板之下不再自动动它，是否失效交人判断。
DECAY_FACTOR = 0.5
WEIGHT_FLOOR = 0.2
# 强化的步进与上限。
REINFORCE_STEP = 0.15
WEIGHT_CEILING = 1.0
# 实体被 Episode 反复提及这么多次、而图谱没有它的边 → 建议补边。
MISSING_MENTION_THRESHOLD = 3


@dataclass
class GraphOp:
    """One proposed change to the graph, with its automation verdict."""

    operation: str
    relation_id: str = ""
    reason: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def auto(self) -> bool:
        return self.operation in AUTO_OPS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "relation_id": self.relation_id,
            "reason": self.reason,
            "auto": self.auto,
            "payload": self.payload,
        }


def _parse_when(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def scan_graph_relations(
    relations: Sequence[Dict[str, Any]],
    *,
    observed_relation_ids: Optional[Dict[str, int]] = None,
    entity_mentions: Optional[Dict[str, int]] = None,
    now: Optional[datetime] = None,
) -> List[GraphOp]:
    """Scan one scope's relations and propose evolution operations.

    Pure: reads rows (the shape :func:`core.memory.graph.list_graph_relations`
    returns) plus this cycle's observations, writes nothing. What may be applied
    automatically is decided by :data:`AUTO_OPS`, not by the caller.
    """
    now = now or datetime.now(timezone.utc)
    observed = observed_relation_ids or {}
    ops: List[GraphOp] = []

    by_functional_key: Dict[tuple, List[Dict[str, Any]]] = {}
    known_entities: set = set()

    for relation in relations:
        relation_id = str(relation.get("relation_id") or "")
        predicate = str(relation.get("predicate") or "").lower()
        source = str(relation.get("source") or "")
        target = str(relation.get("target") or "")
        weight = float(relation.get("weight") or 1.0)
        known_entities.update({source, target} - {""})

        if predicate in FUNCTIONAL_PREDICATES and source:
            by_functional_key.setdefault((source, predicate), []).append(relation)

        hits = int(observed.get(relation_id, 0) or 0)
        if hits > 0:
            # 本周期又被真实使用了：强化。可自动——只上调权重，不改语义。
            ops.append(
                GraphOp(
                    operation=OP_REINFORCE,
                    relation_id=relation_id,
                    reason=f"本周期被 {hits} 个 Episode 检索使用",
                    payload={
                        "weight": min(WEIGHT_CEILING, weight + REINFORCE_STEP * hits)
                    },
                )
            )
            continue

        last_seen = _parse_when(relation.get("last_seen_at"))
        if last_seen is not None and (now - last_seen) > timedelta(days=STALE_DAYS):
            if weight > WEIGHT_FLOOR:
                ops.append(
                    GraphOp(
                        operation=OP_REWEIGHT,
                        relation_id=relation_id,
                        reason=f"超过 {STALE_DAYS} 天未再观察到，降权而非删除",
                        payload={"weight": max(WEIGHT_FLOOR, weight * DECAY_FACTOR)},
                    )
                )
            else:
                # 已经降到地板还没人再观察到：是否失效交人判断。
                ops.append(
                    GraphOp(
                        operation=OP_DEACTIVATE,
                        relation_id=relation_id,
                        reason="权重已到地板且长期未观察，建议人工确认后停用",
                        payload={"weight": weight},
                    )
                )

        if predicate == "alias_of" and relation_id:
            ops.append(
                GraphOp(
                    operation=OP_MERGE_ALIAS,
                    relation_id=relation_id,
                    reason=f"存在别名边 {source} → {target}，建议人工确认后合并实体",
                    payload={"source": source, "target": target},
                )
            )

    # ── 冲突与替代：同一 (source, 功能型谓词) 多个 target ─────────────────────
    for (source, predicate), group in by_functional_key.items():
        targets = {str(r.get("target") or "") for r in group} - {""}
        if len(targets) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda r: (_parse_when(r.get("last_seen_at")) or datetime.min.replace(tzinfo=timezone.utc)),
        )
        newest, oldest = ordered[-1], ordered[0]
        newest_seen = _parse_when(newest.get("last_seen_at"))
        oldest_seen = _parse_when(oldest.get("last_seen_at"))
        stale_gap = (
            newest_seen is not None
            and oldest_seen is not None
            and (newest_seen - oldest_seen) > timedelta(days=STALE_DAYS)
        )
        if stale_gap and int(newest.get("seen_count") or 0) >= int(
            oldest.get("seen_count") or 0
        ):
            # 新关系明显接管：建议替代（人工）。旧边保留、可回滚。
            ops.append(
                GraphOp(
                    operation=OP_SUPERSEDE,
                    relation_id=str(oldest.get("relation_id") or ""),
                    reason=(
                        f"「{source} {predicate}」的目标疑似由"
                        f"「{oldest.get('target')}」更替为「{newest.get('target')}」"
                    ),
                    payload={
                        "superseded_by": str(newest.get("relation_id") or ""),
                        "old_target": str(oldest.get("target") or ""),
                        "new_target": str(newest.get("target") or ""),
                    },
                )
            )
        else:
            ops.append(
                GraphOp(
                    operation=OP_FLAG_CONTRADICTION,
                    relation_id=str(newest.get("relation_id") or ""),
                    reason=(
                        f"「{source} {predicate}」同时指向 {sorted(targets)}，证据无法自行裁决"
                    ),
                    payload={
                        "source": source,
                        "predicate": predicate,
                        "targets": sorted(targets),
                        "relation_ids": [str(r.get("relation_id") or "") for r in group],
                    },
                )
            )

    # ── 缺边建议：Episode 反复提及、图谱却没有的实体 ─────────────────────────
    for entity, mentions in (entity_mentions or {}).items():
        if int(mentions) >= MISSING_MENTION_THRESHOLD and entity not in known_entities:
            ops.append(
                GraphOp(
                    operation=OP_SUGGEST_MISSING,
                    reason=(
                        f"实体「{entity}」在 {mentions} 个 Episode 中被依赖，图谱中没有对应边"
                    ),
                    payload={"entity": entity, "mentions": int(mentions)},
                )
            )

    return ops


def apply_graph_ops(
    ops: Sequence[GraphOp],
    *,
    scope_user_id: str,
) -> Dict[str, Any]:
    """Apply the automatic subset; return the rest for human review.

    The boundary lives here, not in the caller: an op outside :data:`AUTO_OPS`
    is never written to the store no matter who asks.
    """
    applied: List[Dict[str, Any]] = []
    manual: List[Dict[str, Any]] = []
    for op in ops:
        if not op.auto:
            manual.append(op.to_dict())
            continue
        try:
            from core.memory.graph import update_relation_state

            ok = update_relation_state(
                op.relation_id,
                scope_user_id=scope_user_id,
                weight=op.payload.get("weight"),
            )
        except Exception as exc:  # noqa: BLE001 - graph store is optional
            logger.debug("[graph-scan] auto op failed: %s", exc)
            ok = False
        entry = op.to_dict()
        entry["applied"] = bool(ok)
        applied.append(entry)
    return {"applied": applied, "manual": manual}
