"""Compile active ontology packs into the runtime policy for a request."""
from __future__ import annotations
import json
import re
import uuid
from typing import Any, Iterable
from core.ontology.schemas import Constraint, OntologyPackDocument, Workflow


REVIEW_RANK = {"none": 0, "checkpoint": 1, "committee": 2}


def select_workflows(document: OntologyPackDocument, task: str) -> list[Workflow]:
    """Select workflows by case-insensitive lexical trigger match."""
    normalized = task.casefold()
    return [
        workflow
        for workflow in document.workflows
        if any(trigger.casefold() in normalized for trigger in workflow.triggers)
    ]


def workflow_constraints(
    document: OntologyPackDocument,
    workflows: list[Workflow],
) -> list[Constraint]:
    workflow_tools = {
        tool_name
        for workflow in workflows
        for tool_name in (workflow.required_tools + workflow.forbidden_tools)
    }
    workflow_output_tags = {tag for workflow in workflows for tag in workflow.output_tags}
    return [
        rule
        for rule in document.constraints
        if rule.enabled
        and (
            rule.target.tool in workflow_tools
            if rule.target.kind in {"tool", "tool_parameter"}
            else rule.target.output_tag in workflow_output_tags
        )
    ]


def runtime_concepts(
    document: OntologyPackDocument,
    task: str,
    constraints: list[Constraint],
) -> list[dict[str, Any]]:
    """Select task concepts while always retaining concepts used by active rules."""
    by_id = {item.id: item for item in document.concepts}
    selected: list[Any] = []
    selected_ids: set[str] = set()

    def add_with_parents(concept_id: str | None) -> None:
        chain: list[Any] = []
        current = by_id.get(concept_id or "")
        while current is not None and current.id not in selected_ids:
            chain.append(current)
            current = by_id.get(current.parent_id or "")
        for item in reversed(chain):
            if len(selected) >= document.config.max_concepts:
                return
            if item.id not in selected_ids:
                selected.append(item)
                selected_ids.add(item.id)

    for rule in constraints:
        add_with_parents(rule.concept_id)
    for item in select_concepts(document, task, limit=document.config.max_concepts):
        add_with_parents(item["id"])
    return [item.model_dump() for item in selected[: document.config.max_concepts]]


def compile_pack_runtime(
    document: OntologyPackDocument,
    task: str,
    workflows: list[Workflow],
) -> dict[str, Any]:
    constraints = workflow_constraints(document, workflows)
    concepts = runtime_concepts(document, task, constraints)
    selected_concept_ids = {item["id"] for item in concepts}
    relations = [
        relation.model_dump()
        for relation in document.relations
        if relation.subject in selected_concept_ids and relation.object in selected_concept_ids
    ]
    return {
        "pack_id": document.pack_id,
        "version": document.version,
        "name": document.name,
        "domain": document.domain,
        "description": document.description,
        "config": document.config.model_dump(),
        "concepts": concepts,
        "relations": relations,
        "constraints": [item.model_dump(by_alias=True) for item in constraints],
        "workflows": [item.model_dump() for item in workflows],
    }


def select_concepts(
    document: OntologyPackDocument,
    task: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Select only task-relevant concepts; fall back to tagged roots."""
    normalized = task.casefold()
    # English words can be tokenized by boundaries.  Chinese text cannot be
    # treated as one giant ``[\u4e00-\u9fff]+`` token: doing so made a task such
    # as “数据质量分析” match no concept at all.  Concept names and aliases are
    # therefore also checked directly as substrings of the task.
    terms = set(re.findall(r"[a-z0-9_]+", normalized))
    scored: list[tuple[int, Any]] = []
    for concept in document.concepts:
        labels = [concept.id, concept.name, *concept.aliases, *concept.tags]
        haystack = " ".join([*labels, concept.definition]).casefold()
        score = sum(1 for term in terms if len(term) >= 2 and term in haystack)
        score += sum(
            3 for label in labels if len(label.strip()) >= 2 and label.casefold() in normalized
        )
        if score or not concept.parent_id:
            scored.append((score, concept))
    max_items = limit or document.config.max_concepts
    scored.sort(key=lambda item: (-item[0], item[1].id))
    selected = [item for _, item in scored[:max_items]]
    by_id = {item.id: item for item in document.concepts}
    selected_ids = {item.id for item in selected}
    # Preserve the hierarchy for every selected concept when the budget allows.
    for concept in list(selected):
        parent_id = concept.parent_id
        while parent_id and len(selected) < max_items:
            if parent_id not in selected_ids and parent_id in by_id:
                selected.append(by_id[parent_id])
                selected_ids.add(parent_id)
            parent_id = by_id.get(parent_id).parent_id if parent_id in by_id else None
    return [item.model_dump() for item in selected[:max_items]]


def build_runtime_payload(
    documents: list[OntologyPackDocument],
    task: str,
) -> dict[str, Any]:
    """Compile active packs into a compact, serializable request policy."""
    governance_run_id = f"ontog_{uuid.uuid4().hex[:16]}"
    packs: list[dict[str, Any]] = []
    activation_candidates: list[dict[str, Any]] = []
    runtime_events: list[dict[str, Any]] = []
    activated_workflows: list[str] = []
    review_level = "none"
    for document in documents:
        workflows = select_workflows(document, task)
        for workflow in workflows:
            if REVIEW_RANK[workflow.review_level] > REVIEW_RANK[review_level]:
                review_level = workflow.review_level
            ref = f"{document.pack_id}:{workflow.id}"
            activated_workflows.append(ref)
            runtime_events.append(
                {
                    "type": "ontology_activation",
                    "status": "completed",
                    "governance_run_id": governance_run_id,
                    "source": "text",
                    "pack_id": document.pack_id,
                    "workflow_id": workflow.id,
                    "workflow_name": workflow.name,
                    "review_level": workflow.review_level,
                }
            )
        packs.append(compile_pack_runtime(document, task, workflows))
        for workflow in document.workflows:
            if not workflow.asset_triggers:
                continue
            activation_candidates.append(
                {
                    "pack_id": document.pack_id,
                    "workflow_id": workflow.id,
                    "workflow_name": workflow.name,
                    "review_level": workflow.review_level,
                    "asset_triggers": [item.model_dump() for item in workflow.asset_triggers],
                    "pack": compile_pack_runtime(document, task, [workflow]),
                }
            )
    return {
        "enabled": bool(packs),
        "governance_run_id": governance_run_id,
        "packs": packs,
        "review_level": review_level,
        "task": task,
        "activation_candidates": activation_candidates,
        "activated_workflows": activated_workflows,
        "asset_tags": {"tool": {}, "skill": {}, "subagent": {}},
        "runtime_events": runtime_events,
        "review_owner": "outer_workflow",
        "output_review": {"status": "pending", "owner": None, "count": 0},
    }


def register_runtime_asset_tags(
    runtime: dict[str, Any],
    *,
    kind: str,
    asset_id: str,
    tags: Iterable[str],
) -> None:
    """Attach trusted server-side asset metadata to a request runtime."""
    if kind not in {"tool", "skill", "subagent"} or not asset_id:
        return
    catalog = runtime.setdefault("asset_tags", {}).setdefault(kind, {})
    existing = {str(item) for item in catalog.get(asset_id, []) if str(item)}
    existing.update(str(item) for item in tags if str(item))
    catalog[asset_id] = sorted(existing)


def activate_runtime_for_asset(
    runtime: dict[str, Any],
    *,
    kind: str,
    asset_id: str,
    tags: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Monotonically merge workflow policy when an actual asset is invoked."""
    if not runtime.get("enabled") or kind not in {"tool", "skill", "subagent"}:
        return []
    known_tags = set(str(item) for item in tags if str(item))
    known_tags.update(
        str(item)
        for item in runtime.get("asset_tags", {}).get(kind, {}).get(asset_id, [])
        if str(item)
    )
    activated = set(runtime.setdefault("activated_workflows", []))
    events: list[dict[str, Any]] = []
    for candidate in runtime.get("activation_candidates", []):
        ref = f"{candidate.get('pack_id')}:{candidate.get('workflow_id')}"
        if ref in activated:
            continue
        matched_tags: set[str] = set()
        matched = False
        for trigger in candidate.get("asset_triggers", []):
            if trigger.get("kind") != kind:
                continue
            if asset_id in set(trigger.get("ids", [])):
                matched = True
            tag_hits = known_tags & set(trigger.get("tags_any", []))
            if tag_hits:
                matched = True
                matched_tags.update(tag_hits)
        if not matched:
            continue

        fragment = candidate.get("pack") or {}
        current = next(
            (
                pack
                for pack in runtime.get("packs", [])
                if pack.get("pack_id") == fragment.get("pack_id")
            ),
            None,
        )
        if current is None:
            current = fragment
            runtime.setdefault("packs", []).append(current)
        else:
            for field, key in (
                ("workflows", "id"),
                ("constraints", "id"),
                ("concepts", "id"),
                ("relations", "id"),
            ):
                existing_ids = {item.get(key) for item in current.get(field, [])}
                current.setdefault(field, []).extend(
                    item for item in fragment.get(field, []) if item.get(key) not in existing_ids
                )
            if fragment.get("version_id"):
                current["version_id"] = fragment["version_id"]

        level = str(candidate.get("review_level") or "none")
        if REVIEW_RANK.get(level, 0) > REVIEW_RANK.get(runtime.get("review_level"), 0):
            runtime["review_level"] = level
        activated.add(ref)
        runtime["activated_workflows"] = sorted(activated)
        event = {
            "type": "ontology_activation",
            "status": "completed",
            "governance_run_id": runtime.get("governance_run_id"),
            "source": kind,
            "asset_kind": kind,
            "asset_id": asset_id,
            "asset_tags": sorted(known_tags),
            "matched_tags": sorted(matched_tags),
            "pack_id": candidate.get("pack_id"),
            "workflow_id": candidate.get("workflow_id"),
            "workflow_name": candidate.get("workflow_name"),
            "review_level": level,
        }
        runtime.setdefault("runtime_events", []).append(event)
        events.append(event)
    return events


def render_runtime_prompt(runtime: dict[str, Any]) -> str:
    """Render the bounded ontology segment injected into the system prompt."""
    if not runtime.get("enabled"):
        return ""
    compact_packs = []
    for pack in runtime.get("packs", []):
        compact_pack = {
            "id": pack["pack_id"],
            "version": pack["version"],
            "concepts": (
                list(pack.get("concepts", []))
                if pack.get("config", {}).get("injection_enabled", True)
                else []
            ),
            "relations": (
                list(pack.get("relations", []))
                if pack.get("config", {}).get("injection_enabled", True)
                else []
            ),
            "workflows": pack.get("workflows", []),
            "constraints": [
                {
                    "id": rule["id"],
                    "target": rule["target"],
                    "mode": rule["mode"],
                    "message": rule["message"],
                    "suggestion": rule.get("suggestion", ""),
                    "prerequisite_tools": rule.get("prerequisite_tools", []),
                }
                for rule in pack.get("constraints", [])
            ],
        }
        char_budget = int(pack.get("config", {}).get("token_budget", 2500)) * 4
        while len(json.dumps(compact_pack, ensure_ascii=False)) > char_budget:
            if compact_pack["relations"]:
                compact_pack["relations"].pop()
                continue
            if compact_pack["concepts"]:
                compact_pack["concepts"].pop()
                remaining = {item["id"] for item in compact_pack["concepts"]}
                compact_pack["relations"] = [
                    relation
                    for relation in compact_pack["relations"]
                    if relation["subject"] in remaining and relation["object"] in remaining
                ]
                continue
            break
        compact_packs.append(compact_pack)
    compact = {"packs": compact_packs}
    return (
        "<ontology_contract>\n"
        "本轮已启用领域本体校验。以下内容是可执行的领域契约；工具门禁会独立执行，"
        "请在调用前主动满足参数、前置证据与工作流要求。不要虚构证据。\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        + "\n</ontology_contract>"
    )
