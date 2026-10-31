"""Every execution path gets the disabled runtime."""

from __future__ import annotations

from typing import Any


def disabled_ontology_runtime() -> dict[str, Any]:
    return {"enabled": False, "packs": [], "review_level": "none"}


def build_ontology_runtime_for_preference(**_kwargs: Any) -> tuple[bool, dict[str, Any]]:
    return False, disabled_ontology_runtime()


def build_user_ontology_runtime(**_kwargs: Any) -> tuple[bool, dict[str, Any]]:
    return False, disabled_ontology_runtime()


def resolve_runtime_asset_tags(**_kwargs: Any) -> list[str]:
    return []
