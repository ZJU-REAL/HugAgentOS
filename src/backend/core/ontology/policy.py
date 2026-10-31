"""No user or instance can turn ontology validation on."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PluginImportOntologyValidationPolicy:
    user_enabled: bool
    forced: bool

    @property
    def enabled(self) -> bool:
        return False


def user_can_use_ontology_validation(_db, _user_id) -> bool:
    return False
