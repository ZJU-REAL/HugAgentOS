"""分层证据契约 — 技能编译器 V2 的数据面（Layered-evidence skill compiler, P1）.

一个技能候选从四类证据编译而来，四类证据各自决定技能的不同部分，且**只能**
决定那一部分：

* **L1 profile** — 个性化表现。只进策略引用（policy pointer），原始内容永远
  不进入共享技能；个性化在运行时由 L1 绑定。
* **L2 procedure** — 步骤、检查点、失败恢复。它不能独自决定适用范围。
* **L3 graph** — 适用条件、依赖、约束、排除、别名。它永远不能变成执行步骤：
  ``GRAPH_ROLES`` 是封闭集合，没有 ``step``，这是结构上防止图谱事实被误编译
  成 SOP 的地方。
* **Episode** — 有效性。成功率、真实工具序列、失败案例。单次成功只进观察池。

Pack 是**内容寻址、不可变**的：同一份证据重复解析产生同一个 ``pack_id``，
所以重复运行不会重复创建候选，而任何激活技能都能沿着 pack 反查到准确的
L2 / L3 / Episode 标识。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

PACK_SCHEMA_VERSION = "2"
MANIFEST_SCHEMA_VERSION = "2"

# ── Evidence layers ──────────────────────────────────────────────────────────

LAYER_PROFILE = "profile"
LAYER_PROCEDURE = "procedure"
LAYER_GRAPH = "graph"
LAYER_EPISODE = "episode"
EVIDENCE_LAYERS = (LAYER_PROFILE, LAYER_PROCEDURE, LAYER_GRAPH, LAYER_EPISODE)

# ── Graph roles: what an L3 relation may contribute to a skill ───────────────
# A closed set, and ``step`` is deliberately absent: a graph fact may bound
# where a skill applies, never tell the agent what to do.

ROLE_APPLICABILITY = "applicability"
ROLE_DEPENDENCY = "dependency"
ROLE_CONSTRAINT = "constraint"
ROLE_EXCLUSION = "exclusion"
ROLE_ALIAS = "alias"
GRAPH_ROLES = frozenset(
    {ROLE_APPLICABILITY, ROLE_DEPENDENCY, ROLE_CONSTRAINT, ROLE_EXCLUSION, ROLE_ALIAS}
)

# How a predicate defaults into a role when the resolver has nothing better.
_PREDICATE_ROLE = {
    "depends_on": ROLE_DEPENDENCY,
    "uses": ROLE_DEPENDENCY,
    "requires": ROLE_DEPENDENCY,
    "constrained_by": ROLE_CONSTRAINT,
    "alias_of": ROLE_ALIAS,
}


def role_for_predicate(predicate: str) -> str:
    return _PREDICATE_ROLE.get(str(predicate or "").strip().lower(), ROLE_APPLICABILITY)


# ── EvidenceRef ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceRef:
    """统一证据引用：任何层的一条证据，都能被稳定反查。

    ``external_id`` 是所属存储自己的稳定 id（Episode id / mref_* / grel_*）；
    ``content_hash`` 是内容哈希，外部 id 失效时的兜底键。两者一起构成
    「可审计」：证据来自哪一层、哪个租户/工作区/用户、可信度多少，都在引用里。
    """

    layer: str
    external_id: str = ""
    content_hash: str = ""
    tenant_id: str = "default"
    workspace_id: str = "default"
    user_id: str = ""
    confidentiality: str = "private"
    confidence: float = 0.0
    observed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer": self.layer,
            "external_id": self.external_id,
            "content_hash": self.content_hash,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "confidentiality": self.confidentiality,
            "confidence": round(float(self.confidence), 4),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EvidenceRef":
        return cls(
            layer=str(raw.get("layer") or ""),
            external_id=str(raw.get("external_id") or ""),
            content_hash=str(raw.get("content_hash") or ""),
            tenant_id=str(raw.get("tenant_id") or "default"),
            workspace_id=str(raw.get("workspace_id") or "default"),
            user_id=str(raw.get("user_id") or ""),
            confidentiality=str(raw.get("confidentiality") or "private"),
            confidence=float(raw.get("confidence") or 0.0),
            observed_at=str(raw.get("observed_at") or ""),
        )


# ── CapabilityEvidencePackV2 ─────────────────────────────────────────────────


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class CapabilityEvidencePackV2:
    """一次技能编译允许看到的全部证据，分层、内容寻址、不可变。"""

    candidate_scope: Dict[str, Any] = field(default_factory=dict)
    intent_cluster: Dict[str, Any] = field(default_factory=dict)

    # L1：只有策略引用，永远没有原始内容。
    profile_policy_refs: List[Dict[str, Any]] = field(default_factory=list)
    # L2：每条带 EvidenceRef 与内容（rule/why/applies_to）——内容进文档，引用进血缘。
    procedures: List[Dict[str, Any]] = field(default_factory=list)
    # L3：每条必须带 relation_id 与 role（GRAPH_ROLES 之一）。
    graph_context: List[Dict[str, Any]] = field(default_factory=list)
    # Episode：引用 + 结果 + 工具序列，供有效性与 replay 覆盖判断。
    episodes: List[Dict[str, Any]] = field(default_factory=list)

    successful_sequences: List[List[str]] = field(default_factory=list)
    ordering_constraints: List[Dict[str, Any]] = field(default_factory=list)
    failure_recoveries: List[Dict[str, Any]] = field(default_factory=list)
    negative_examples: List[Dict[str, Any]] = field(default_factory=list)

    required_tools: List[str] = field(default_factory=list)
    forbidden_tools: List[str] = field(default_factory=list)

    support: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = PACK_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_scope": self.candidate_scope,
            "intent_cluster": self.intent_cluster,
            "profile_policy_refs": self.profile_policy_refs,
            "procedures": self.procedures,
            "graph_context": self.graph_context,
            "episodes": self.episodes,
            "successful_sequences": self.successful_sequences,
            "ordering_constraints": self.ordering_constraints,
            "failure_recoveries": self.failure_recoveries,
            "negative_examples": self.negative_examples,
            "required_tools": self.required_tools,
            "forbidden_tools": self.forbidden_tools,
            "support": self.support,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CapabilityEvidencePackV2":
        return cls(
            candidate_scope=raw.get("candidate_scope") or {},
            intent_cluster=raw.get("intent_cluster") or {},
            profile_policy_refs=raw.get("profile_policy_refs") or [],
            procedures=raw.get("procedures") or [],
            graph_context=raw.get("graph_context") or [],
            episodes=raw.get("episodes") or [],
            successful_sequences=raw.get("successful_sequences") or [],
            ordering_constraints=raw.get("ordering_constraints") or [],
            failure_recoveries=raw.get("failure_recoveries") or [],
            negative_examples=raw.get("negative_examples") or [],
            required_tools=raw.get("required_tools") or [],
            forbidden_tools=raw.get("forbidden_tools") or [],
            support=raw.get("support") or {},
            schema_version=str(raw.get("schema_version") or PACK_SCHEMA_VERSION),
        )

    def content_hash(self) -> str:
        """确定性哈希：同一证据 → 同一 pack，重复运行不重复建候选。"""
        return "sha256:" + hashlib.sha256(
            _canonical(self.to_dict()).encode("utf-8")
        ).hexdigest()

    @property
    def pack_id(self) -> str:
        return "pack_" + self.content_hash()[len("sha256:") :][:24]

    # ── Validation ──────────────────────────────────────────────────────────

    def validate(self) -> List[str]:
        """结构校验，返回违规列表。空列表 = 合格。"""
        violations: List[str] = []
        for relation in self.graph_context:
            role = str(relation.get("role") or "")
            if role not in GRAPH_ROLES:
                # ``step`` 落在这里：图谱事实永远不能变成执行步骤。
                violations.append(f"graph_role_not_allowed:{role or 'missing'}")
            if not str(relation.get("relation_id") or ""):
                violations.append("graph_relation_missing_id")
        for ref in self.profile_policy_refs:
            if "content" in ref or "raw" in ref:
                # L1 只能以策略引用形式出现；带原文即违规。
                violations.append("profile_raw_content_in_pack")
        scope_level = str((self.candidate_scope or {}).get("level") or "workspace")
        if scope_level != "user":
            for procedure in self.procedures:
                conf = str((procedure.get("ref") or {}).get("confidentiality") or "")
                if conf == "classified":
                    violations.append("classified_procedure_in_shared_pack")
        return violations

    def graph_conflicts(self) -> List[Dict[str, Any]]:
        """同一主体 + 功能型谓词出现不同 target → 冲突，候选不得自动激活。"""
        functional = {"depends_on", "belongs_to", "responsible_for", "located_in"}
        seen: Dict[tuple, str] = {}
        conflicts: List[Dict[str, Any]] = []
        for relation in self.graph_context:
            predicate = str(relation.get("predicate") or "").lower()
            if predicate not in functional:
                continue
            key = (str(relation.get("source") or ""), predicate)
            target = str(relation.get("target") or "")
            if not key[0] or not target:
                continue
            previous = seen.get(key)
            if previous is not None and previous != target:
                conflicts.append(
                    {"source": key[0], "predicate": predicate, "targets": [previous, target]}
                )
            else:
                seen[key] = target
        return conflicts


# ── Deterministic manifest（技能的机器可读边界，运行时读它而不是读正文） ─────


def build_manifest(
    pack: CapabilityEvidencePackV2,
    *,
    scope_level: str,
    risk_tier: str,
) -> Dict[str, Any]:
    """由代码从 pack 生成确定性结构——LLM 只写正文，不决定这里的任何一项。

    L3 只进 ``applies_when`` / ``excludes_when`` / ``dependencies`` /
    ``required_graph_relations``，绝不进步骤。
    """
    applies: List[Dict[str, Any]] = []
    excludes: List[Dict[str, Any]] = []
    dependencies: List[Dict[str, Any]] = []
    required_relations: List[str] = []

    for relation in pack.graph_context:
        role = str(relation.get("role") or "")
        entry = {
            "relation_id": str(relation.get("relation_id") or ""),
            "source": str(relation.get("source") or ""),
            "predicate": str(relation.get("predicate") or ""),
            "target": str(relation.get("target") or ""),
        }
        if entry["relation_id"]:
            required_relations.append(entry["relation_id"])
        if role == ROLE_EXCLUSION:
            excludes.append(entry)
        elif role in (ROLE_DEPENDENCY, ROLE_CONSTRAINT):
            dependencies.append({**entry, "role": role})
        elif role == ROLE_APPLICABILITY:
            applies.append(entry)
        # alias 只用于实体归一，不进适用条件。

    for task_type in pack.intent_cluster.get("task_types") or []:
        applies.append({"task_type": str(task_type)})
    for task_type in pack.intent_cluster.get("excluded_task_types") or []:
        excludes.append({"task_type": str(task_type)})

    conflicts = pack.graph_conflicts()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "scope": scope_level,
        "applies_when": applies,
        "excludes_when": excludes,
        "required_graph_relations": sorted(set(required_relations)),
        "required_tools": list(pack.required_tools),
        "forbidden_tools": list(pack.forbidden_tools),
        "dependencies": dependencies,
        "profile_policy_refs": list(pack.profile_policy_refs),
        "evidence_pack_id": pack.pack_id,
        "evidence_pack_hash": pack.content_hash(),
        "risk_tier": risk_tier,
        "graph_conflict": bool(conflicts),
        "graph_conflicts": conflicts,
    }


# ── Cross-layer candidate validation（强制校验，任何一条失败即拒绝生成） ─────

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@"),  # 带凭据的 URL
)


def _steps_section(body: str) -> str:
    match = re.search(r"^## 步骤\s*$(.*?)(?=^## |\Z)", body or "", re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def validate_candidate_structure(
    *,
    pack: CapabilityEvidencePackV2,
    manifest: Dict[str, Any],
    document_tools: Sequence[str],
    body: str,
    scope_level: str,
) -> List[str]:
    """技能候选的分层边界校验。返回违规列表，空即通过。

    每一条对应验收标准里的一项，缺一不可：

    * 技能工具 ⊆ 证据中实际出现的工具（防止授权扩散）；
    * L3 relation 没有被写成执行步骤；
    * 共享技能不含 L1 原文；
    * 无密钥 / Token / 凭据；
    * L2 与 L3 矛盾时拒绝生成；
    * manifest 的 pack 哈希可复算（引用可解析）。
    """
    violations = list(pack.validate())

    evidence_tools = set(pack.required_tools)
    widened = sorted(set(str(t) for t in document_tools) - evidence_tools)
    if widened:
        violations.append(f"tool_not_in_evidence:{widened}")

    steps = _steps_section(body)
    if steps:
        for relation in pack.graph_context:
            source = str(relation.get("source") or "")
            target = str(relation.get("target") or "")
            relationship = str(relation.get("relationship") or "")
            # 图谱事实以「A ... 关系 ... B」的形态整体出现在步骤里，
            # 即说明它被当成了 SOP 而不是边界。
            if source and target and relationship and (
                f"{source}{relationship}{target}" in steps.replace(" ", "")
            ):
                violations.append(
                    f"graph_relation_written_as_step:{relation.get('relation_id')}"
                )

    for pattern in _SECRET_PATTERNS:
        if pattern.search(body or ""):
            violations.append("secret_material_in_body")
            break

    if scope_level != "user":
        for ref in manifest.get("profile_policy_refs") or []:
            if "content" in ref or "raw" in ref:
                violations.append("profile_raw_content_in_shared_skill")

    # L2 与 L3 矛盾：某个做法声明适用于 X，而图谱证据把 X 排除在外。
    excluded_names = {
        str(e.get("task_type") or e.get("target") or e.get("source") or "")
        for e in manifest.get("excludes_when") or []
    } - {""}
    for procedure in pack.procedures:
        applies_to = str(procedure.get("applies_to") or "")
        if applies_to and applies_to in excluded_names:
            violations.append(f"l2_l3_contradiction:{applies_to}")

    if str(manifest.get("evidence_pack_hash") or "") != pack.content_hash():
        violations.append("evidence_pack_hash_mismatch")

    return violations
