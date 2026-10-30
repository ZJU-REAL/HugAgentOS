"""Capability builds are never checked against Domain Packs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BuildValidationReport:
    valid: bool
    errors: list[Any] = field(default_factory=list)
    warnings: list[Any] = field(default_factory=list)


def ensure_ontology_build_valid(*_args: Any, **_kwargs: Any) -> BuildValidationReport:
    return BuildValidationReport(valid=True)
