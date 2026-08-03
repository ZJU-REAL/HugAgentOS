"""Neo4j-backed L3 graph memory.

mem0ai 2.x removed graph-store support from its open-source ``Memory`` class.
The project still owns a Neo4j service and an L3 product contract, so this
module keeps that contract local and explicit instead of passing an ignored
``graph_store`` key to mem0.

L3 stores stable, declarative relationships (entity -- predicate -- entity).
It deliberately does not store procedures (L2), profile preferences (L1),
volatile measurements, or raw conversation text.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from core.config.settings import settings
from core.memory.context import MemoryContext

logger = logging.getLogger(__name__)


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

_MAX_ENTITY_CHARS = 120
_MAX_RELATIONS_PER_TURN = 12
_MIN_CONFIDENCE = 0.65
_SAFE_NAME = re.compile(r"\s+")


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
        return PREDICATE_LABELS[self.predicate]

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
    source = _clean_name(raw.get("source"))
    target = _clean_name(raw.get("target"))
    predicate = str(raw.get("predicate") or "").strip().lower()
    if not source or not target or source == target or predicate not in PREDICATE_LABELS:
        return None

    source_type = str(raw.get("source_type") or "other").strip().lower()
    target_type = str(raw.get("target_type") or "other").strip().lower()
    if source_type not in ALLOWED_ENTITY_TYPES:
        source_type = "other"
    if target_type not in ALLOWED_ENTITY_TYPES:
        target_type = "other"
    try:
        confidence = float(raw.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = max(0.0, min(1.0, confidence))
    if confidence < _MIN_CONFIDENCE:
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
            _entity_key(relation.source, relation.source_type),
            relation.predicate,
            _entity_key(relation.target, relation.target_type),
        )
        if key in seen:
            continue
        seen.add(key)
        relations.append(relation)
        if len(relations) >= _MAX_RELATIONS_PER_TURN:
            break
    return relations


def _clean_name(value: Any) -> str:
    name = unicodedata.normalize("NFKC", str(value or ""))
    name = _SAFE_NAME.sub(" ", name).strip(" \t\r\n,，。；;：:")
    if len(name) < 2 or len(name) > _MAX_ENTITY_CHARS:
        return ""
    return name


def _normalise_name(value: str) -> str:
    return _SAFE_NAME.sub(" ", unicodedata.normalize("NFKC", value).lower()).strip()


def _entity_key(name: str, entity_type: str) -> str:
    raw = f"{entity_type}|{_normalise_name(name)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def relation_id_for(relation: GraphRelation, *, scope_user_id: str, workspace_id: str) -> str:
    raw = "|".join(
        (
            scope_user_id,
            workspace_id or "default",
            _entity_key(relation.source, relation.source_type),
            relation.predicate,
            _entity_key(relation.target, relation.target_type),
        )
    )
    return "grel_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:28]


_driver = None
_driver_lock = threading.Lock()
_schema_ready = False


def _get_driver():
    """Lazily create the thread-safe Neo4j driver; failures are retryable."""
    global _driver, _schema_ready
    if not (settings.memory.enabled and settings.memory.graph_enabled):
        return None
    if _driver is not None:
        return _driver
    with _driver_lock:
        if _driver is not None:
            return _driver
        try:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(
                settings.memory.neo4j_url,
                auth=(settings.memory.neo4j_username, settings.memory.neo4j_password),
                connection_timeout=3.0,
            )
            driver.verify_connectivity()
            _driver = driver
            if not _schema_ready:
                _ensure_schema(driver)
                _schema_ready = True
            logger.info("[GraphMemory] Neo4j L3 initialized")
            return _driver
        except Exception as exc:  # noqa: BLE001 - optional memory layer
            logger.warning(
                "[GraphMemory] initialization failed; L3 disabled for this attempt: %s", exc
            )
            _driver = None
            return None


def _reset_driver() -> None:
    global _driver, _schema_ready
    with _driver_lock:
        current, _driver = _driver, None
        _schema_ready = False
    if current is not None:
        try:
            current.close()
        except Exception:
            pass


def _ensure_schema(driver) -> None:
    statements = (
        "CREATE CONSTRAINT memory_entity_scope_key IF NOT EXISTS "
        "FOR (n:MemoryEntity) REQUIRE (n.scope_user_id, n.workspace_id, n.key) IS UNIQUE",
        "CREATE INDEX memory_relation_id IF NOT EXISTS "
        "FOR ()-[r:MEMORY_RELATION]-() ON (r.relation_id)",
    )
    try:
        with driver.session() as session:
            for statement in statements:
                session.run(statement).consume()
    except Exception as exc:  # Schema acceleration must not make L3 unavailable.
        logger.warning("[GraphMemory] schema setup skipped: %s", exc)


_UPSERT_CYPHER = """
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
    r.evidence_hash = $evidence_hash
RETURN r.relation_id AS relation_id, r.seen_count AS seen_count
"""


def _write_sync(ctx: MemoryContext, relations: Sequence[GraphRelation]) -> list[Dict[str, Any]]:
    driver = _get_driver()
    if driver is None:
        return []
    evidence_hash = hashlib.sha256(
        f"{ctx.chat_id or ''}|{ctx.message_id or ''}".encode("utf-8")
    ).hexdigest()
    rows: list[Dict[str, Any]] = []
    try:
        with driver.session() as session:
            for relation in relations:
                relation_id = relation_id_for(
                    relation,
                    scope_user_id=ctx.effective_scope_user_id,
                    workspace_id=ctx.workspace_id,
                )
                record = session.run(
                    _UPSERT_CYPHER,
                    scope_user_id=ctx.effective_scope_user_id,
                    workspace_id=ctx.workspace_id or "default",
                    source_key=_entity_key(relation.source, relation.source_type),
                    source=relation.source,
                    source_type=relation.source_type,
                    target_key=_entity_key(relation.target, relation.target_type),
                    target=relation.target,
                    target_type=relation.target_type,
                    relation_id=relation_id,
                    predicate=relation.predicate,
                    relationship=relation.relationship,
                    confidence=relation.confidence,
                    author_user_id=ctx.user_id,
                    chat_id=ctx.chat_id or "",
                    evidence_hash=evidence_hash,
                ).single()
                if record is None:
                    continue
                row = relation.to_dict()
                row.update(
                    {
                        "relation_id": str(record["relation_id"]),
                        "seen_count": int(record["seen_count"] or 1),
                    }
                )
                rows.append(row)
        return rows
    except Exception as exc:  # noqa: BLE001 - L3 is optional and post-response
        logger.warning("[GraphMemory] write failed: %s", exc)
        _reset_driver()
        return []


async def write_graph_relations(
    ctx: MemoryContext, relations: Sequence[GraphRelation]
) -> list[Dict[str, Any]]:
    """Persist validated relations without blocking the event loop."""
    if not relations:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _write_sync, ctx, list(relations))


_LIST_CYPHER = """
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


_SEARCH_CYPHER = """
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


def _read_sync(
    *, scope_user_id: str, workspace_id: str, limit: int, query: str = ""
) -> list[Dict[str, Any]]:
    driver = _get_driver()
    if driver is None:
        return []
    statement = _SEARCH_CYPHER if query.strip() else _LIST_CYPHER
    try:
        with driver.session() as session:
            result = session.run(
                statement,
                scope_user_id=scope_user_id,
                workspace_id=workspace_id or "default",
                search_text=query.strip(),
                limit=max(1, min(int(limit), 200)),
            )
            return [dict(record) for record in result]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GraphMemory] read failed: %s", exc)
        _reset_driver()
        return []


async def list_graph_relations(
    scope_user_id: str, *, workspace_id: str = "default", limit: int = 30
) -> list[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _read_sync(
            scope_user_id=scope_user_id,
            workspace_id=workspace_id,
            limit=limit,
        ),
    )


async def search_graph_relations(
    scope_user_id: str,
    query: str,
    *,
    workspace_id: str = "default",
    limit: int = 5,
) -> list[Dict[str, Any]]:
    if not query.strip():
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _read_sync(
            scope_user_id=scope_user_id,
            workspace_id=workspace_id,
            limit=limit,
            query=query,
        ),
    )


def relation_is_active(relation_id: str, *, scope_user_id: str) -> Optional[bool]:
    """Whether a relation still exists and is active. ``None`` = could not tell.

    Used by the evolution runtime to decide whether a skill whose manifest
    depends on this relation is still applicable. Tri-state on purpose: an
    unreachable graph store must not disable every dependent skill.
    """
    driver = _get_driver()
    if driver is None or not relation_id:
        return None
    try:
        with driver.session() as session:
            record = session.run(
                """
                MATCH (s:MemoryEntity)-[r:MEMORY_RELATION {relation_id: $relation_id}]->()
                WHERE s.scope_user_id = $scope_user_id
                RETURN coalesce(r.active, true) AS active
                """,
                relation_id=relation_id,
                scope_user_id=scope_user_id,
            ).single()
            if record is None:
                return False
            return bool(record["active"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GraphMemory] relation_is_active failed: %s", exc)
        _reset_driver()
        return None


def update_relation_state(
    relation_id: str,
    *,
    scope_user_id: str,
    weight: Optional[float] = None,
    active: Optional[bool] = None,
) -> bool:
    """Adjust one relation's weight and/or active flag — never delete.

    This is the write surface graph evolution is allowed: down-weighting and
    deactivating are reversible; deletion is not, and a wrongly deleted edge is
    indistinguishable from knowledge the system never had.
    """
    driver = _get_driver()
    if driver is None or not relation_id:
        return False
    sets = []
    parameters: Dict[str, Any] = {
        "relation_id": relation_id,
        "scope_user_id": scope_user_id,
    }
    if weight is not None:
        sets.append("r.weight = $weight")
        parameters["weight"] = max(0.0, min(1.0, float(weight)))
    if active is not None:
        sets.append("r.active = $active")
        parameters["active"] = bool(active)
    if not sets:
        return False
    try:
        with driver.session() as session:
            record = session.run(
                "MATCH (s:MemoryEntity)-[r:MEMORY_RELATION {relation_id: $relation_id}]->() "
                "WHERE s.scope_user_id = $scope_user_id "
                f"SET {', '.join(sets)} "
                "RETURN count(r) AS touched",
                **parameters,
            ).single()
            return bool(record and int(record["touched"] or 0) > 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GraphMemory] update_relation_state failed: %s", exc)
        _reset_driver()
        return False


def _delete_sync(relation_id: str, *, scope_user_id: str) -> bool:
    driver = _get_driver()
    if driver is None:
        return False
    try:
        with driver.session() as session:
            record = session.run(
                """
                MATCH (s:MemoryEntity)-[r:MEMORY_RELATION {relation_id: $relation_id}]->(t)
                WHERE s.scope_user_id = $scope_user_id
                DELETE r
                WITH s, t, count(*) AS deleted
                OPTIONAL MATCH (s)-[sr]-()
                WITH s, t, deleted, count(sr) AS s_degree
                FOREACH (_ IN CASE WHEN s_degree = 0 THEN [1] ELSE [] END | DELETE s)
                WITH t, deleted
                OPTIONAL MATCH (t)-[tr]-()
                WITH t, deleted, count(tr) AS t_degree
                FOREACH (_ IN CASE WHEN t_degree = 0 THEN [1] ELSE [] END | DELETE t)
                RETURN deleted
                """,
                relation_id=relation_id,
                scope_user_id=scope_user_id,
            ).single()
            return bool(record and int(record["deleted"] or 0) > 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GraphMemory] delete failed: %s", exc)
        _reset_driver()
        return False


async def delete_graph_relation(relation_id: str, *, scope_user_id: str) -> bool:
    if not relation_id:
        return False
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _delete_sync(relation_id, scope_user_id=scope_user_id)
    )
