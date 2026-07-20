"""Domain Pack validation and deterministic runtime gates."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from core.ontology.schemas import Constraint, OntologyPackDocument, Workflow
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as PydanticValidationError


@dataclass(slots=True)
class ValidationIssue:
    severity: str
    path: str
    message: str


@dataclass(slots=True)
class ValidationReport:
    valid: bool
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [asdict(item) for item in self.errors],
            "warnings": [asdict(item) for item in self.warnings],
        }


@dataclass(slots=True)
class OntologyGateDecision:
    allowed: bool
    decision: str
    violations: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    matched_rule_ids: list[str] = field(default_factory=list)


class DomainPackValidator:
    """Validate structure, JSON Schema, and tool references before activation."""

    def validate(
        self,
        payload: dict[str, Any],
        *,
        tool_schemas: dict[str, dict[str, Any]] | None = None,
        known_tools: Iterable[str] = (),
    ) -> tuple[OntologyPackDocument | None, ValidationReport]:
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        try:
            document = OntologyPackDocument.model_validate(payload)
        except PydanticValidationError as exc:
            for item in exc.errors(include_url=False):
                errors.append(
                    ValidationIssue(
                        "error",
                        ".".join(str(part) for part in item["loc"]),
                        item["msg"],
                    )
                )
            return None, ValidationReport(False, errors, warnings)

        schemas = tool_schemas or {}
        known = set(known_tools) | set(schemas)
        for index, rule in enumerate(document.constraints):
            path = f"constraints.{index}"
            if rule.schema_:
                try:
                    Draft202012Validator.check_schema(rule.schema_)
                except Exception as exc:  # SchemaError is version-specific
                    errors.append(ValidationIssue("error", f"{path}.schema", str(exc)))
            tool_name = rule.target.tool
            if not tool_name:
                continue
            if tool_name not in known:
                issue = ValidationIssue(
                    "warning" if document.config.allow_unresolved_tools else "error",
                    f"{path}.target.tool",
                    f"unknown tool reference: {tool_name}",
                )
                (warnings if document.config.allow_unresolved_tools else errors).append(issue)
                continue
            if rule.target.kind == "tool_parameter" and tool_name in schemas:
                properties = _tool_input_schema(schemas[tool_name]).get("properties", {})
                if rule.target.parameter not in properties:
                    errors.append(
                        ValidationIssue(
                            "error",
                            f"{path}.target.parameter",
                            f"unknown parameter {rule.target.parameter!r} for tool {tool_name}",
                        )
                    )

        for index, workflow in enumerate(document.workflows):
            for tool_name in set(workflow.required_tools + workflow.forbidden_tools):
                if tool_name not in known:
                    issue = ValidationIssue(
                        "warning" if document.config.allow_unresolved_tools else "error",
                        f"workflows.{index}",
                        f"unknown tool reference: {tool_name}",
                    )
                    (warnings if document.config.allow_unresolved_tools else errors).append(issue)
            overlap = set(workflow.required_tools) & set(workflow.forbidden_tools)
            if overlap:
                errors.append(
                    ValidationIssue(
                        "error",
                        f"workflows.{index}",
                        f"tools cannot be both required and forbidden: {sorted(overlap)}",
                    )
                )
            for trigger_index, trigger in enumerate(workflow.asset_triggers):
                if trigger.kind != "tool":
                    continue
                for tool_name in trigger.ids:
                    if tool_name in known:
                        continue
                    issue = ValidationIssue(
                        "warning" if document.config.allow_unresolved_tools else "error",
                        f"workflows.{index}.asset_triggers.{trigger_index}.ids",
                        f"unknown tool reference: {tool_name}",
                    )
                    (warnings if document.config.allow_unresolved_tools else errors).append(issue)

        return document, ValidationReport(not errors, errors, warnings)


def _tool_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    function = schema.get("function") if isinstance(schema, dict) else None
    if isinstance(function, dict):
        return function.get("parameters") or function.get("inputSchema") or {}
    return schema.get("inputSchema") or schema.get("parameters") or schema


def select_workflows(document: OntologyPackDocument, task: str) -> list[Workflow]:
    """Select workflows by case-insensitive lexical trigger match."""
    normalized = task.casefold()
    return [
        workflow
        for workflow in document.workflows
        if any(trigger.casefold() in normalized for trigger in workflow.triggers)
    ]


_REVIEW_RANK = {"none": 0, "checkpoint": 1, "committee": 2}


def _workflow_constraints(
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


def _runtime_concepts(
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


def _compile_pack_runtime(
    document: OntologyPackDocument,
    task: str,
    workflows: list[Workflow],
) -> dict[str, Any]:
    constraints = _workflow_constraints(document, workflows)
    concepts = _runtime_concepts(document, task, constraints)
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
    # as “企业风险分析” match no concept at all.  Concept names and aliases are
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
            if _REVIEW_RANK[workflow.review_level] > _REVIEW_RANK[review_level]:
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
        packs.append(_compile_pack_runtime(document, task, workflows))
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
                    "pack": _compile_pack_runtime(document, task, [workflow]),
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
        if _REVIEW_RANK.get(level, 0) > _REVIEW_RANK.get(runtime.get("review_level"), 0):
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
                removed = compact_pack["concepts"].pop()
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


def requires_output_review(runtime: dict[str, Any]) -> bool:
    """Whether an L-b/L-c review must run after the streamed draft."""
    return bool(
        runtime.get("enabled")
        and (
            runtime.get("review_level") != "none"
            or any(
                (rule.get("target") or {}).get("kind") == "output"
                for pack in runtime.get("packs", [])
                for rule in pack.get("constraints", [])
            )
        )
    )


def claim_output_review(runtime: dict[str, Any], *, owner: str) -> bool:
    """Atomically claim the single final-answer review for one governance run."""
    if not requires_output_review(runtime):
        return False
    state = runtime.setdefault(
        "output_review",
        {"status": "pending", "owner": None, "count": 0},
    )
    if state.get("status") in {"running", "completed"}:
        return False
    state.update({"status": "running", "owner": owner})
    return True


def complete_output_review(
    runtime: dict[str, Any],
    *,
    owner: str,
    verdict: str,
    attempts: int = 1,
) -> None:
    """Mark the claimed review complete and retain its single-run evidence."""
    state = runtime.setdefault("output_review", {})
    if state.get("status") != "running" or state.get("owner") != owner:
        return
    count = int(state.get("count") or 0) + max(1, int(attempts or 1))
    state.update(
        {
            "status": "completed",
            "owner": owner,
            "count": count,
            "verdict": verdict,
        }
    )


def release_output_review(runtime: dict[str, Any], *, owner: str) -> None:
    """Release a failed claim so a safe retry can review the final answer."""
    state = runtime.setdefault("output_review", {})
    if state.get("status") == "running" and state.get("owner") == owner:
        state.update({"status": "pending", "owner": None})


def evaluate_tool_call(
    runtime: dict[str, Any],
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    completed_tools: Iterable[str] = (),
) -> OntologyGateDecision:
    """Apply zero-LLM L-a validation to one tool call."""
    if not runtime.get("enabled"):
        return OntologyGateDecision(True, "pass")
    completed = set(completed_tools)
    violations: list[dict[str, Any]] = []
    suggestions: list[str] = []
    matched: list[str] = []
    enforced = False

    for pack in runtime.get("packs", []):
        for workflow in pack.get("workflows", []):
            if tool_name in workflow.get("forbidden_tools", []):
                violations.append(
                    {
                        "rule_id": f"workflow:{workflow['id']}:forbidden",
                        "message": f"工作流 {workflow['name']} 禁止调用工具 {tool_name}",
                        "mode": "enforce",
                    }
                )
                enforced = True
        for rule in pack.get("constraints", []):
            target = rule.get("target") or {}
            if target.get("tool") != tool_name:
                continue
            matched.append(rule["id"])
            missing_prerequisites = [
                name for name in rule.get("prerequisite_tools", []) if name not in completed
            ]
            rule_errors: list[str] = []
            if missing_prerequisites:
                rule_errors.append("缺少前置工具调用: " + ", ".join(missing_prerequisites))
            try:
                if target.get("kind") == "tool":
                    Draft202012Validator(rule.get("schema") or {}).validate(tool_input)
                elif target.get("kind") == "tool_parameter":
                    parameter = target.get("parameter")
                    Draft202012Validator(rule.get("schema") or {}).validate(
                        tool_input.get(parameter)
                    )
            except ValidationError as exc:
                rule_errors.append(exc.message)
            if rule_errors:
                violations.append(
                    {
                        "pack_id": pack.get("pack_id"),
                        "rule_id": rule["id"],
                        "message": rule["message"],
                        "reasons": rule_errors,
                        "mode": rule.get("mode", "log"),
                        "risk": rule.get("risk", "low"),
                    }
                )
                if rule.get("suggestion"):
                    suggestions.append(rule["suggestion"])
                if rule.get("mode") == "enforce":
                    enforced = True

    if not violations:
        return OntologyGateDecision(True, "pass", matched_rule_ids=matched)
    return OntologyGateDecision(
        allowed=not enforced,
        decision="deny" if enforced else "log",
        violations=violations,
        suggestions=suggestions,
        matched_rule_ids=matched,
    )


def evaluate_output(
    runtime: dict[str, Any],
    *,
    answer: str,
    citations: Iterable[dict[str, Any]] = (),
    completed_tools: Iterable[str] | None = (),
) -> OntologyGateDecision:
    """Apply deterministic output constraints before any LLM reviewer."""
    if not runtime.get("enabled"):
        return OntologyGateDecision(True, "pass")
    citation_items = list(citations)
    # ``None`` means the caller cannot observe a complete tool trace (the
    # legacy non-streaming reply path).  Output and citation rules still run,
    # but required-tool rules must not produce a false omission claim.
    completed = set(completed_tools) if completed_tools is not None else None
    violations: list[dict[str, Any]] = []
    matched: list[str] = []
    suggestions: list[str] = []
    enforced = False
    for pack in runtime.get("packs", []):
        if completed is not None:
            for workflow in pack.get("workflows", []):
                missing_tools = sorted(set(workflow.get("required_tools", [])) - completed)
                if missing_tools:
                    violations.append(
                        {
                            "pack_id": pack.get("pack_id"),
                            "rule_id": f"workflow:{workflow.get('id')}:required",
                            "message": "领域工作流缺少必需的证据工具调用。",
                            "reasons": ["未调用: " + ", ".join(missing_tools)],
                            "mode": "enforce",
                            "risk": workflow.get("risk", "low"),
                        }
                    )
                    enforced = True
        output_tags = {
            tag for workflow in pack.get("workflows", []) for tag in workflow.get("output_tags", [])
        }
        for rule in pack.get("constraints", []):
            target = rule.get("target") or {}
            if target.get("kind") != "output":
                continue
            if output_tags and target.get("output_tag") not in output_tags:
                continue
            matched.append(rule["id"])
            reasons: list[str] = []
            if rule.get("requires_citations") and not citation_items:
                reasons.append("输出缺少可追溯引用证据")
            try:
                Draft202012Validator(rule.get("schema") or {}).validate(answer)
            except ValidationError as exc:
                reasons.append(exc.message)
            if reasons:
                violations.append(
                    {
                        "pack_id": pack.get("pack_id"),
                        "rule_id": rule["id"],
                        "message": rule["message"],
                        "reasons": reasons,
                        "mode": rule.get("mode", "log"),
                        "risk": rule.get("risk", "low"),
                    }
                )
                if rule.get("suggestion"):
                    suggestions.append(rule["suggestion"])
                if rule.get("mode") == "enforce":
                    enforced = True
    if not violations:
        return OntologyGateDecision(True, "pass", matched_rule_ids=matched)
    return OntologyGateDecision(
        allowed=not enforced,
        decision="deny" if enforced else "log",
        violations=violations,
        suggestions=suggestions,
        matched_rule_ids=matched,
    )
