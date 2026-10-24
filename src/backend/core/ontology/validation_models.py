"""Validation issues, reports and gate decisions."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any

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
