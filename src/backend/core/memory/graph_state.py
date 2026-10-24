"""Graph vocabulary, limits, Cypher queries and the shared driver."""
from __future__ import annotations
import re
import threading
from dataclasses import dataclass, field
from typing import Dict

PREDICATE_LABELS: Dict[str, str] = {
    "is_a": "属于类别",
    "part_of": "组成",
    "belongs_to": "隶属于",
    "responsible_for": "负责",
    "depends_on": "依赖",
    "uses": "使用",
    "alias_of": "别名为",
    "located_in": "位于",
    "produces": "产出",
    "constrained_by": "受约束于",
    "related_to": "关联",
}


ALLOWED_ENTITY_TYPES = frozenset(
    {
        "person",
        "organization",
        "team",
        "project",
        "system",
        "document",
        "metric",
        "concept",
        "place",
        "other",
    }
)


MAX_ENTITY_CHARS = 120


MAX_RELATIONS_PER_TURN = 12


MIN_CONFIDENCE = 0.65


SAFE_NAME = re.compile(r"\s+")


UPSERT_CYPHER = """
MERGE (s:MemoryEntity {
  scope_user_id: $scope_user_id, workspace_id: $workspace_id, key: $source_key
})
ON CREATE SET s.created_at = datetime()
SET s.name = $source, s.entity_type = $source_type, s.last_seen_at = datetime()
MERGE (t:MemoryEntity {
  scope_user_id: $scope_user_id, workspace_id: $workspace_id, key: $target_key
})
ON CREATE SET t.created_at = datetime()
SET t.name = $target, t.entity_type = $target_type, t.last_seen_at = datetime()
MERGE (s)-[r:MEMORY_RELATION {relation_id: $relation_id}]->(t)
ON CREATE SET r.created_at = datetime(), r.seen_count = 0, r.weight = 1.0, r.active = true
WITH r,
     CASE
       WHEN $effect_id <> '' AND $effect_id IN coalesce(r.outbox_effect_ids, []) THEN true
       ELSE false
     END AS replayed
FOREACH (_ IN CASE WHEN replayed THEN [] ELSE [1] END |
  SET r.predicate = $predicate,
      r.relationship = $relationship,
      r.last_seen_at = datetime(),
      r.seen_count = coalesce(r.seen_count, 0) + 1,
      r.confidence = CASE
        WHEN coalesce(r.confidence, 0.0) > $confidence THEN r.confidence
        ELSE $confidence
      END,
      r.author_user_id = $author_user_id,
      r.chat_id = $chat_id,
      r.evidence_hash = $evidence_hash,
      r.outbox_effect_ids = CASE
        WHEN $effect_id = '' THEN coalesce(r.outbox_effect_ids, [])
        ELSE coalesce(r.outbox_effect_ids, []) + [$effect_id]
      END
)
RETURN r.relation_id AS relation_id, r.seen_count AS seen_count, replayed
"""


LIST_CYPHER = """
MATCH (s:MemoryEntity)-[r:MEMORY_RELATION]->(t:MemoryEntity)
WHERE s.scope_user_id = $scope_user_id
  AND s.workspace_id = $workspace_id
  AND coalesce(r.active, true) = true
RETURN r.relation_id AS relation_id,
       s.name AS source, s.entity_type AS source_type,
       r.predicate AS predicate, r.relationship AS relationship,
       t.name AS target, t.entity_type AS target_type,
       coalesce(r.confidence, 0.0) AS confidence,
       coalesce(r.seen_count, 1) AS seen_count,
       coalesce(r.weight, 1.0) AS weight,
       toString(r.last_seen_at) AS last_seen_at
ORDER BY coalesce(r.weight, 1.0) DESC, coalesce(r.seen_count, 1) DESC, r.last_seen_at DESC
LIMIT $limit
"""


SEARCH_CYPHER = """
MATCH (s:MemoryEntity)-[r:MEMORY_RELATION]->(t:MemoryEntity)
WHERE s.scope_user_id = $scope_user_id
  AND s.workspace_id = $workspace_id
  AND coalesce(r.active, true) = true
  AND (
    toLower($search_text) CONTAINS toLower(s.name)
    OR toLower($search_text) CONTAINS toLower(t.name)
  )
RETURN r.relation_id AS relation_id,
       s.name AS source, s.entity_type AS source_type,
       r.predicate AS predicate, r.relationship AS relationship,
       t.name AS target, t.entity_type AS target_type,
       coalesce(r.confidence, 0.0) AS confidence,
       coalesce(r.seen_count, 1) AS seen_count,
       coalesce(r.weight, 1.0) AS weight,
       toString(r.last_seen_at) AS last_seen_at
ORDER BY coalesce(r.weight, 1.0) DESC, coalesce(r.seen_count, 1) DESC, r.last_seen_at DESC
LIMIT $limit
"""


@dataclass
class GraphState:
    driver: object = None
    driver_lock: object = field(default_factory=lambda: threading.Lock())
    schema_ready: object = False


_STATE = GraphState()


def process_state() -> GraphState:
    return _STATE
