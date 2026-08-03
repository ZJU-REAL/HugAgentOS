"""Evidence Resolver — 把分散在四个存储里的证据解析成一个不可变 pack（P2）.

上游（意图聚类 / 工具序列模式）只知道 Episode 的 id 和它检索过什么；这里把
它们变成 :class:`CapabilityEvidencePackV2`：

* Episode 证据直接来自 episode views（PostgreSQL 已经装配好的形态）；
* L2 procedures 由 loop 侧解析（内容 + mref），这里补上 :class:`EvidenceRef`；
* L3 relations 来自 Episode 记录的检索事件（relation_id / predicate /
  confidence 都在事件负载里），实体文本从 ref-shadow 的 preview 兜底解析——
  Neo4j 不可用时证据仍然可反查，只是少了展示文本；
* L1 永远只投影成策略引用。共享技能里出现任何一条 L1 原文都是违规，
  由 ``pack.validate()`` 拒绝。

隔离在这里执行：pack 只收 scope 覆盖的 Episode / 记忆 / 关系。产出内容寻址，
同一批证据永远解析出同一个 ``pack_id``。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from core.evolution.evidence_contract import (
    LAYER_GRAPH,
    LAYER_PROCEDURE,
    CapabilityEvidencePackV2,
    EvidenceRef,
    role_for_predicate,
)

logger = logging.getLogger(__name__)

# 一个 pack 携带的每层证据上限。够重建决策，又不至于让 pack 自己变成隐私面。
_MAX_EPISODES = 20
_MAX_RELATIONS = 20
_MAX_PROCEDURES = 12
_MAX_FAILURES = 8


def _relation_display(
    relation: Dict[str, Any], *, user_id: str, workspace_id: str
) -> Dict[str, str]:
    """(source, relationship, target) for one traced relation.

    The trace event deliberately carries refs, not entity text. The shadow row's
    preview is exactly ``"A → 关系 → B"``, so it recovers the display text
    without touching Neo4j — and degrades to empty strings, never to a failure.
    """
    content_hash = str(relation.get("content_hash") or "")
    if not content_hash:
        return {"source": "", "relationship": "", "target": ""}
    try:
        from core.memory.ref_shadow import resolve_ref

        row = resolve_ref(
            user_id=user_id,
            content_hash=content_hash,
            layer=LAYER_GRAPH,
            workspace_id=workspace_id,
        )
        preview = str(getattr(row, "content_preview", "") or "")
        parts = [p.strip() for p in preview.split("→")]
        if len(parts) == 3 and all(parts):
            return {"source": parts[0], "relationship": parts[1], "target": parts[2]}
    except Exception as exc:  # noqa: BLE001 - display text is best-effort
        logger.debug("[evidence-resolver] relation display lookup failed: %s", exc)
    return {"source": "", "relationship": "", "target": ""}


def _episode_entry(view: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "episode_id": str(view.get("episode_id") or ""),
        "chat_id": str(view.get("chat_id") or ""),
        "user_id": str(view.get("user_id") or ""),
        "verdict": str(view.get("verdict") or "unknown"),
        "task_type": str(view.get("task_type") or "chat"),
        "tool_sequence": [str(t) for t in (view.get("tool_sequence") or [])][:16],
        "objective": str(view.get("objective") or "")[:160],
        # Replay 覆盖判断需要区分「带 L3 依赖」与「不带依赖」两类场景。
        "used_graph": bool(view.get("graph_relations") or view.get("graph_refs")),
    }


def resolve_capability_pack(
    *,
    views: Sequence[Dict[str, Any]],
    intent_cluster: Optional[Dict[str, Any]] = None,
    procedures: Sequence[Dict[str, Any]] = (),
    ordering_constraints: Sequence[Dict[str, Any]] = (),
    scope: Optional[Dict[str, str]] = None,
    tenant_id: str = "default",
) -> CapabilityEvidencePackV2:
    """Resolve one candidate's evidence into an immutable, content-addressed pack.

    ``views`` are the supporting episode views (already restricted to the
    cluster / pattern). Tenant isolation is enforced here: an episode from a
    different tenant is dropped, never silently included — a workspace skill
    must not be compiled from another tenant's behaviour.
    """
    scope = dict(scope or {"level": "workspace"})
    scope_level = str(scope.get("level") or "workspace")

    episodes: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    negatives: List[Dict[str, Any]] = []
    sequences: List[List[str]] = []
    tools: List[str] = []
    users: set = set()
    workspaces: set = set()
    chats: set = set()
    signatures: set = set()

    for view in views:
        view_tenant = str(view.get("tenant_id") or "default")
        if view_tenant != (tenant_id or "default"):
            # 跨租户证据直接丢弃：隔离是 resolver 的责任，不是调用方的默契。
            continue
        entry = _episode_entry(view)
        episodes.append(entry)
        users.add(entry["user_id"])
        chats.add(entry["chat_id"] or entry["episode_id"])
        workspaces.add(str(view.get("workspace_id") or "default"))
        sequence = entry["tool_sequence"]
        if sequence:
            signatures.add("→".join(sequence))
        if entry["verdict"] == "success":
            if sequence and sequence not in sequences:
                sequences.append(sequence)
            for tool in sequence:
                if tool not in tools:
                    tools.append(tool)
        elif entry["verdict"] == "failed":
            failures.append(
                {
                    "episode_id": entry["episode_id"],
                    "objective": entry["objective"],
                    "tool_sequence": sequence[:12],
                    "failed_at": str(view.get("failed_tool") or ""),
                    "error": str(view.get("error") or "")[:200],
                }
            )
            negatives.append(entry)

    # ── L3：Episode 检索到过的图谱关系，带稳定 relation_id ────────────────────
    graph_context: List[Dict[str, Any]] = []
    seen_relations: set = set()
    for view in views:
        user_id = str(view.get("user_id") or "")
        for relation in view.get("graph_relations") or []:
            relation_id = str(relation.get("relation_id") or "")
            content_hash = str(relation.get("content_hash") or "")
            key = relation_id or content_hash
            if not key or key in seen_relations:
                continue
            seen_relations.add(key)
            predicate = str(relation.get("predicate") or "")
            workspace_id = str(relation.get("workspace_id") or "default")
            display = _relation_display(
                relation, user_id=user_id, workspace_id=workspace_id
            )
            graph_context.append(
                {
                    "relation_id": relation_id,
                    "content_hash": content_hash,
                    "predicate": predicate,
                    "role": role_for_predicate(predicate),
                    "confidence": float(relation.get("confidence") or 0.0),
                    "workspace_id": workspace_id,
                    **display,
                }
            )
            if len(graph_context) >= _MAX_RELATIONS:
                break
        if len(graph_context) >= _MAX_RELATIONS:
            break

    # ── L2：procedures 补齐 EvidenceRef（引用进血缘，内容进文档） ─────────────
    typed_procedures: List[Dict[str, Any]] = []
    for procedure in list(procedures)[:_MAX_PROCEDURES]:
        ref = EvidenceRef(
            layer=LAYER_PROCEDURE,
            external_id=str(procedure.get("ref") or ""),
            content_hash=str(procedure.get("content_hash") or ""),
            tenant_id=tenant_id or "default",
            workspace_id=str(procedure.get("workspace_id") or "default"),
            user_id=str(procedure.get("user_id") or ""),
            confidentiality=str(procedure.get("confidentiality") or "private"),
        )
        typed_procedures.append(
            {
                "ref": ref.to_dict(),
                "rule": str(procedure.get("rule") or ""),
                "why": str(procedure.get("why") or ""),
                "applies_to": str(procedure.get("applies_to") or ""),
                "user_id": ref.user_id,
            }
        )

    # ── L1：只投影策略引用。个性化在运行时绑定，绝不进共享技能正文。──────────
    profile_policy_refs: List[Dict[str, Any]] = []
    if users - {""}:
        profile_policy_refs.append(
            {
                "policy": "bind_user_profile_at_runtime",
                "reason": "输出语言/语气等个性化由 L1 在运行时绑定，不写入技能",
            }
        )

    cluster = dict(intent_cluster or {})
    support = {
        "occasions": len(chats - {""}) or len(episodes),
        "success_rate": round(
            sum(1 for e in episodes if e["verdict"] == "success") / len(episodes), 4
        )
        if episodes
        else 0.0,
        "plan_variety": len(signatures),
        "workspace_count": len(workspaces - {""}) or 1,
        "user_count": len(users - {""}),
    }

    return CapabilityEvidencePackV2(
        candidate_scope={"level": scope_level, "tenant_id": tenant_id or "default", **{
            k: v for k, v in scope.items() if k not in ("level",)
        }},
        intent_cluster={
            "representative": str(cluster.get("representative") or "")[:160],
            "task_types": sorted(
                {e["task_type"] for e in episodes if e["task_type"]}
            ),
            "excluded_task_types": [
                str(t)
                for rule in ordering_constraints
                for t in (rule.get("contradicted_in") or [])
            ],
        },
        profile_policy_refs=profile_policy_refs,
        procedures=typed_procedures,
        graph_context=graph_context,
        episodes=episodes[:_MAX_EPISODES],
        successful_sequences=sequences[:6],
        ordering_constraints=[dict(rule) for rule in ordering_constraints],
        failure_recoveries=failures[:_MAX_FAILURES],
        negative_examples=negatives[:_MAX_FAILURES],
        required_tools=tools,
        forbidden_tools=[],
        support=support,
    )
