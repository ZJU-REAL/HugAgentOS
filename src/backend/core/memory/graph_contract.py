"""Validate and normalise extracted graph relations."""
from __future__ import annotations
import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence
from core.memory import graph_state

@dataclass(frozen=True)
class GraphRelation:
    """One validated L3 relation before persistence."""

    source: str
    predicate: str
    target: str
    source_type: str = "other"
    target_type: str = "other"
    confidence: float = 0.8

    @property
    def relationship(self) -> str:
        return graph_state.PREDICATE_LABELS[self.predicate]

    @property
    def text(self) -> str:
        return f"{self.source} → {self.relationship} → {self.target}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "source_type": self.source_type,
            "predicate": self.predicate,
            "relationship": self.relationship,
            "target": self.target,
            "target_type": self.target_type,
            "confidence": self.confidence,
        }


def parse_relation(raw: Any) -> Optional[GraphRelation]:
    """Validate an extractor item and return its canonical representation."""
    if not isinstance(raw, dict):
        return None
    source = clean_name(raw.get("source"))
    target = clean_name(raw.get("target"))
    predicate = str(raw.get("predicate") or "").strip().lower()
    if not source or not target or source == target or predicate not in graph_state.PREDICATE_LABELS:
        return None

    source_type = str(raw.get("source_type") or "other").strip().lower()
    target_type = str(raw.get("target_type") or "other").strip().lower()
    if source_type not in graph_state.ALLOWED_ENTITY_TYPES:
        source_type = "other"
    if target_type not in graph_state.ALLOWED_ENTITY_TYPES:
        target_type = "other"
    try:
        confidence = float(raw.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = max(0.0, min(1.0, confidence))
    if confidence < graph_state.MIN_CONFIDENCE:
        return None
    return GraphRelation(
        source=source,
        predicate=predicate,
        target=target,
        source_type=source_type,
        target_type=target_type,
        confidence=confidence,
    )


def parse_relations(raw_items: Sequence[Any]) -> list[GraphRelation]:
    """Validate, de-duplicate, and bound one turn's extracted relations."""
    relations: list[GraphRelation] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_items or []:
        relation = parse_relation(raw)
        if relation is None:
            continue
        key = (
            entity_key(relation.source, relation.source_type),
            relation.predicate,
            entity_key(relation.target, relation.target_type),
        )
        if key in seen:
            continue
        seen.add(key)
        relations.append(relation)
        if len(relations) >= graph_state.MAX_RELATIONS_PER_TURN:
            break
    return relations


def clean_name(value: Any) -> str:
    name = unicodedata.normalize("NFKC", str(value or ""))
    name = graph_state.SAFE_NAME.sub(" ", name).strip(" \t\r\n,，。；;：:")
    if len(name) < 2 or len(name) > graph_state.MAX_ENTITY_CHARS:
        return ""
    return name


def normalise_name(value: str) -> str:
    return graph_state.SAFE_NAME.sub(" ", unicodedata.normalize("NFKC", value).lower()).strip()


def entity_key(name: str, entity_type: str) -> str:
    raw = f"{entity_type}|{normalise_name(name)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def relation_id_for(relation: GraphRelation, *, scope_user_id: str, workspace_id: str) -> str:
    raw = "|".join(
        (
            scope_user_id,
            workspace_id or "default",
            entity_key(relation.source, relation.source_type),
            relation.predicate,
            entity_key(relation.target, relation.target_type),
        )
    )
    return "grel_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:28]
