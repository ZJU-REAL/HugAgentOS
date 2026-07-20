"""Typed, JSON-serializable schema for ontology Domain Packs.

The schema deliberately stays lightweight.  A Domain Pack is an operational
contract for an agent harness, not an OWL reasoner: concepts and relations make
the vocabulary explicit, constraints are executable JSON-Schema fragments,
and workflows decide which gates/review levels apply to a task.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = str
RiskLevel = Literal["low", "medium", "high"]
RuleMode = Literal["log", "enforce"]
ReviewLevel = Literal["none", "checkpoint", "committee"]
AssetKind = Literal["tool", "skill", "subagent"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Concept(StrictModel):
    id: Identifier = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    name: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list)
    definition: str = Field(min_length=1, max_length=4000)
    parent_id: Identifier | None = None
    closed_values: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    risk: RiskLevel = "low"


class Relation(StrictModel):
    id: Identifier = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    subject: Identifier
    predicate: str = Field(min_length=1, max_length=128)
    object: Identifier
    description: str = Field(default="", max_length=2000)
    min_cardinality: int | None = Field(default=None, ge=0)
    max_cardinality: int | None = Field(default=None, ge=0)
    forbidden: bool = False

    @model_validator(mode="after")
    def validate_cardinality(self) -> "Relation":
        if (
            self.min_cardinality is not None
            and self.max_cardinality is not None
            and self.min_cardinality > self.max_cardinality
        ):
            raise ValueError("min_cardinality cannot exceed max_cardinality")
        return self


class ConstraintTarget(StrictModel):
    kind: Literal["tool", "tool_parameter", "output"]
    tool: str | None = Field(default=None, max_length=255)
    parameter: str | None = Field(default=None, max_length=255)
    output_tag: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_target(self) -> "ConstraintTarget":
        if self.kind in {"tool", "tool_parameter"} and not self.tool:
            raise ValueError(f"{self.kind} target requires tool")
        if self.kind == "tool_parameter" and not self.parameter:
            raise ValueError("tool_parameter target requires parameter")
        if self.kind == "output" and not self.output_tag:
            raise ValueError("output target requires output_tag")
        return self


class Constraint(StrictModel):
    id: Identifier = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    name: str = Field(min_length=1, max_length=255)
    target: ConstraintTarget
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    concept_id: Identifier | None = None
    requires_citations: bool = False
    prerequisite_tools: list[str] = Field(default_factory=list)
    mode: RuleMode = "log"
    risk: RiskLevel = "low"
    message: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(default="", max_length=2000)
    enabled: bool = True


class AssetTrigger(StrictModel):
    """Activate a workflow when a governed runtime asset is actually invoked."""

    kind: AssetKind
    ids: list[str] = Field(default_factory=list)
    tags_any: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_matcher(self) -> "AssetTrigger":
        self.ids = [item for item in self.ids if item]
        self.tags_any = [item for item in self.tags_any if item]
        if not self.ids and not self.tags_any:
            raise ValueError("asset trigger requires ids or tags_any")
        return self


class Workflow(StrictModel):
    id: Identifier = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    name: str = Field(min_length=1, max_length=255)
    triggers: list[str] = Field(default_factory=list)
    asset_triggers: list[AssetTrigger] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    output_tags: list[str] = Field(default_factory=list)
    review_level: ReviewLevel = "none"
    risk: RiskLevel = "low"

    @model_validator(mode="after")
    def validate_activation(self) -> "Workflow":
        if not self.triggers and not self.asset_triggers:
            raise ValueError("workflow requires text triggers or asset_triggers")
        return self


class PackConfig(StrictModel):
    injection_enabled: bool = True
    max_concepts: int = Field(default=12, ge=1, le=50)
    token_budget: int = Field(default=2500, ge=256, le=16000)
    committee_size: int = Field(default=3, ge=2, le=5)
    repeated_denial_threshold: int = Field(default=2, ge=1, le=10)
    circuit_breaker_threshold: int = Field(default=5, ge=2, le=50)
    allow_unresolved_tools: bool = False


class OntologyPackDocument(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    pack_id: Identifier = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=255)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    domain: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    config: PackConfig = Field(default_factory=PackConfig)
    concepts: list[Concept] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    workflows: list[Workflow] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "OntologyPackDocument":
        self._require_unique("concept", [item.id for item in self.concepts])
        self._require_unique("relation", [item.id for item in self.relations])
        self._require_unique("constraint", [item.id for item in self.constraints])
        self._require_unique("workflow", [item.id for item in self.workflows])

        concept_ids = {item.id for item in self.concepts}
        for concept in self.concepts:
            if concept.parent_id and concept.parent_id not in concept_ids:
                raise ValueError(
                    f"concept {concept.id} references unknown parent {concept.parent_id}"
                )
        for relation in self.relations:
            for endpoint in (relation.subject, relation.object):
                if endpoint not in concept_ids:
                    raise ValueError(
                        f"relation {relation.id} references unknown concept {endpoint}"
                    )
        for constraint in self.constraints:
            if constraint.concept_id and constraint.concept_id not in concept_ids:
                raise ValueError(
                    f"constraint {constraint.id} references unknown concept "
                    f"{constraint.concept_id}"
                )
        self._validate_parent_cycles()
        return self

    @staticmethod
    def _require_unique(kind: str, ids: list[str]) -> None:
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate {kind} ids: {', '.join(duplicates)}")

    def _validate_parent_cycles(self) -> None:
        parent_by_id = {item.id: item.parent_id for item in self.concepts}
        for concept_id in parent_by_id:
            seen: set[str] = set()
            current: str | None = concept_id
            while current:
                if current in seen:
                    raise ValueError(f"concept hierarchy contains a cycle at {current}")
                seen.add(current)
                current = parent_by_id.get(current)
