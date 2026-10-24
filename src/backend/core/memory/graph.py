"""Read and write L3 graph relations in Neo4j."""
from __future__ import annotations
import asyncio
import hashlib
from typing import Any, Dict, Optional, Sequence
from core.config.settings import settings
from core.memory.context import MemoryContext
from core.memory.executor import run_effect
import logging
from core.memory import graph_contract
from core.memory import graph_state

logger = logging.getLogger(__name__)

def get_driver():
    """Lazily create the thread-safe Neo4j driver; failures are retryable."""
    if not (settings.memory.enabled and settings.memory.graph_enabled):
        return None
    if graph_state.process_state().driver is not None:
        return graph_state.process_state().driver
    with graph_state.process_state().driver_lock:
        if graph_state.process_state().driver is not None:
            return graph_state.process_state().driver
        try:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(
                settings.memory.neo4j_url,
                auth=(settings.memory.neo4j_username, settings.memory.neo4j_password),
                connection_timeout=3.0,
            )
            driver.verify_connectivity()
            graph_state.process_state().driver = driver
            if not graph_state.process_state().schema_ready:
                ensure_schema(driver)
                graph_state.process_state().schema_ready = True
            logger.info("[GraphMemory] Neo4j L3 initialized")
            return graph_state.process_state().driver
        except Exception as exc:  # noqa: BLE001 - optional memory layer
            logger.warning(
                "[GraphMemory] initialization failed; L3 disabled for this attempt: %s", exc
            )
            graph_state.process_state().driver = None
            return None


def reset_driver() -> None:
    with graph_state.process_state().driver_lock:
        current, graph_state.process_state().driver = graph_state.process_state().driver, None
        graph_state.process_state().schema_ready = False
    if current is not None:
        try:
            current.close()
        except Exception as fallback_error:
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.memory.graph:_reset_driver', fallback_error)


def ensure_schema(driver) -> None:
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


def write_sync(ctx: MemoryContext, relations: Sequence[graph_contract.GraphRelation]) -> list[Dict[str, Any]]:
    driver = get_driver()
    if driver is None:
        return []
    evidence_hash = hashlib.sha256(
        f"{ctx.chat_id or ''}|{ctx.message_id or ''}".encode("utf-8")
    ).hexdigest()
    rows: list[Dict[str, Any]] = []
    try:
        with driver.session() as session:
            for relation in relations:
                relation_id = graph_contract.relation_id_for(
                    relation,
                    scope_user_id=ctx.effective_scope_user_id,
                    workspace_id=ctx.workspace_id,
                )
                record = session.run(
                    graph_state.UPSERT_CYPHER,
                    scope_user_id=ctx.effective_scope_user_id,
                    workspace_id=ctx.workspace_id or "default",
                    source_key=graph_contract.entity_key(relation.source, relation.source_type),
                    source=relation.source,
                    source_type=relation.source_type,
                    target_key=graph_contract.entity_key(relation.target, relation.target_type),
                    target=relation.target,
                    target_type=relation.target_type,
                    relation_id=relation_id,
                    predicate=relation.predicate,
                    relationship=relation.relationship,
                    confidence=relation.confidence,
                    author_user_id=ctx.user_id,
                    chat_id=ctx.chat_id or "",
                    evidence_hash=evidence_hash,
                    effect_id=ctx.effect_id or "",
                ).single()
                if record is None:
                    continue
                row = relation.to_dict()
                row.update(
                    {
                        "relation_id": str(record["relation_id"]),
                        "seen_count": int(record["seen_count"] or 1),
                        "replayed": bool(record["replayed"]),
                    }
                )
                rows.append(row)
        return rows
    except Exception as exc:  # noqa: BLE001 - L3 is optional and post-response
        logger.warning("[GraphMemory] write failed: %s", exc)
        reset_driver()
        return []


async def write_graph_relations(
    ctx: MemoryContext, relations: Sequence[graph_contract.GraphRelation]
) -> list[Dict[str, Any]]:
    """Persist validated relations without blocking the event loop."""
    if not relations:
        return []
    return await run_effect(write_sync, ctx, list(relations))


def read_sync(
    *, scope_user_id: str, workspace_id: str, limit: int, query: str = ""
) -> list[Dict[str, Any]]:
    driver = get_driver()
    if driver is None:
        return []
    statement = graph_state.SEARCH_CYPHER if query.strip() else graph_state.LIST_CYPHER
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
        reset_driver()
        return []


async def list_graph_relations(
    scope_user_id: str, *, workspace_id: str = "default", limit: int = 30
) -> list[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: read_sync(
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
        lambda: read_sync(
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
    driver = get_driver()
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
        reset_driver()
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
    driver = get_driver()
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
        reset_driver()
        return False


def delete_sync(relation_id: str, *, scope_user_id: str) -> bool:
    driver = get_driver()
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
        reset_driver()
        return False


async def delete_graph_relation(relation_id: str, *, scope_user_id: str) -> bool:
    if not relation_id:
        return False
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: delete_sync(relation_id, scope_user_id=scope_user_id)
    )
