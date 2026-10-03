"""Memory backend configuration, singleton lifecycle and vector reconciliation."""

from __future__ import annotations
import logging
import threading
from typing import Optional
from core.config.settings import settings

logger = logging.getLogger(__name__)

# Migration scripts and runtime share this collection name.
MEMORY_COLLECTION_NAME = "hugagent_memories"
# L2 stores procedures; fact extraction belongs to the layered pipeline.
MEMORY_TYPE_PROCEDURAL = "procedural"
# One lock and instance per process; account epoch changes invalidate the cache.
_memory_instance = None
_memory_epoch: Optional[str] = None
_memory_lock = threading.Lock()
_memory_init_failed = False
_embedding_patched = False
_MIGRATION_MAX_ROWS = 10_000


def _patch_mem0_embedding() -> None:
    """Patch mem0 OpenAIEmbedding to remove dimensions param.

    qwen3_embedding_8b does not support the matryoshka dimensions parameter,
    but mem0's OpenAIEmbedding.embed() hardcodes dimensions=...
    Patched only once.
    """
    global _embedding_patched
    if _embedding_patched:
        return
    try:
        from mem0.embeddings.openai import OpenAIEmbedding as _OAIEmbed

        def _patched_embed(self, text, memory_action=None):
            text = text.replace("\n", " ")
            return (
                self.client.embeddings.create(input=[text], model=self.config.model)
                .data[0]
                .embedding
            )

        _OAIEmbed.embed = _patched_embed
        _embedding_patched = True
    except Exception:
        pass


def _build_mem0_config() -> dict:
    """
    Assemble the mem0 config:
    - LLM: from DB (memory role) or env fallback
    - Embedder: from DB (embedding role) or env fallback
    - Vector Store: Milvus
    L3 Neo4j configuration is owned by ``core.memory.graph`` rather than mem0.
    """
    # Resolve LLM config from DB
    try:
        from core.services.model_config import ModelConfigService

        svc = ModelConfigService.get_instance()
        mem_cfg = svc.resolve("memory")
        embed_cfg = svc.resolve("embedding")
    except Exception:
        mem_cfg = None
        embed_cfg = None

    llm_model = mem_cfg.model_name if mem_cfg else settings.memory.model_name
    llm_url = mem_cfg.base_url if mem_cfg else settings.memory.model_url
    llm_key = mem_cfg.api_key if mem_cfg else settings.memory.api_key

    embed_model = embed_cfg.model_name if embed_cfg else settings.memory.embed_model
    embed_url = embed_cfg.base_url if embed_cfg else settings.memory.embed_url
    embed_key = embed_cfg.api_key if embed_cfg else settings.memory.embed_api_key
    embed_dims = int(
        (embed_cfg.extra.get("dimensions") if embed_cfg else None) or settings.memory.embed_dims
    )

    config: dict = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": llm_model,
                "openai_base_url": llm_url,
                "api_key": llm_key,
                "temperature": 0.1,
                "max_tokens": 2000,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": embed_model,
                "openai_base_url": embed_url,
                "api_key": embed_key,
            },
        },
        "vector_store": {
            "provider": "milvus",
            "config": {
                "url": settings.memory.milvus_url,
                "token": settings.memory.milvus_token,
                "collection_name": MEMORY_COLLECTION_NAME,
                "embedding_model_dims": embed_dims,
                # The default L2 metric + mem0 treating distance as score would rank "less
                # similar first". qwen3_embedding_8b outputs L2-normalized vectors, so COSINE
                # is equivalent to IP; COSINE is the more intuitive choice. Existing
                # collections' indexes need a one-off migration via rebuild_index_metric.py.
                "metric_type": "COSINE",
            },
        },
        "version": "v1.1",
        # No ``custom_fact_extraction_prompt``. It configured mem0's own
        # LLM pass over whatever it is handed — a *fact* extractor, which is
        # precisely the thing L2 no longer stores. Every write now goes in with
        # ``infer=False`` after our procedural extractor has done the judging,
        # so this prompt could only ever run as a second opinion nobody asked
        # for. Leaving it configured would suggest a code path that no longer
        # exists.
    }

    return config


def _apply_probed_embed_dims(cfg: dict) -> None:
    """Correct the collection dimension with one real /embeddings call.

    The embed call is patched to drop the ``dimensions`` parameter (qwen3-style
    models reject matryoshka), so stored vectors always come back at the model's
    *native* width, while the collection is created from configuration (default
    1024). When the two disagree the collection is born broken (qwen3-embedding-8b
    actually returns 4096) and every subsequent insert fails on a dim mismatch.
    A probe failure (network / credentials) never blocks init — the configured
    value stays in effect.
    """
    emb = cfg["embedder"]["config"]
    vs = cfg["vector_store"]["config"]
    try:
        from openai import OpenAI
        from core.services.desktop_model_credentials import sync_client_kwargs

        client = OpenAI(
            api_key=emb["api_key"],
            base_url=emb["openai_base_url"],
            **sync_client_kwargs(emb["api_key"], emb["openai_base_url"], timeout=8),
            timeout=8,
            max_retries=0,
        )
        probed = len(
            client.embeddings.create(input=["dimension probe"], model=emb["model"])
            .data[0]
            .embedding
        )
    except Exception as exc:
        logger.warning(
            "[MemoryService] embed dims probe failed, keeping configured %s: %s",
            vs["embedding_model_dims"],
            exc,
        )
        return
    if probed and probed != vs["embedding_model_dims"]:
        logger.info(
            "[MemoryService] embedding model returns %d dims (configured %d), using the probed value",
            probed,
            vs["embedding_model_dims"],
        )
        vs["embedding_model_dims"] = probed


def _reconcile_vector_collection(cfg: dict) -> Optional[list]:
    """Reconcile an existing Milvus collection whose dim no longer matches.

    Two ways to get here: the collection was bootstrapped at a wrong default
    width (writes never succeeded, so it is empty), or the user switched to an
    embedding model with a different native width (the data is real and must
    follow the new model).

    - empty collection → drop it; mem0 recreates it at the right width.
    - non-empty → re-embed every stored text with the *new* embedder first;
      only when all of them succeed is the old collection dropped. Returns the
      re-embedded rows for `_replay_migrated_rows` to insert after mem0 has
      recreated the collection. If re-embedding fails the old data stays put.

    Reconcile failures only warn — init proceeds exactly as before.
    """
    vs = cfg["vector_store"]["config"]
    client = None
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=vs["url"], token=vs.get("token") or "")
        name = vs["collection_name"]
        if not client.has_collection(name):
            return None
        desc = client.describe_collection(name)
        existing = 0
        for field in desc.get("fields", []):
            dim = (field.get("params") or {}).get("dim")
            if dim:
                existing = int(dim)
                break
        desired = int(vs["embedding_model_dims"])
        if not existing or existing == desired:
            return None
        rows = int(client.get_collection_stats(name).get("row_count", 0) or 0)
        if rows == 0:
            client.drop_collection(name)
            logger.warning(
                "[MemoryService] empty collection %s at %d dims != target %d — dropped for rebuild",
                name,
                existing,
                desired,
            )
            return None
        if rows > _MIGRATION_MAX_ROWS:
            logger.error(
                "[MemoryService] collection %s holds %d rows at %d dims != target %d — "
                "too large for automatic migration, migrate it manually",
                name,
                rows,
                existing,
                desired,
            )
            return None
        replay = _reembed_rows(client, name, cfg)
        if replay is None:
            # Old data is untouched; writes keep failing until the new embedder
            # is reachable, at which point the next init retries the migration.
            logger.error(
                "[MemoryService] collection %s needs migration from %d to %d dims but "
                "re-embedding failed — keeping the old collection",
                name,
                existing,
                desired,
            )
            return None
        client.drop_collection(name)
        logger.warning(
            "[MemoryService] migrating collection %s: %d memories re-embedded from %d to %d dims",
            name,
            len(replay),
            existing,
            desired,
        )
        return replay
    except Exception as exc:
        logger.warning("[MemoryService] collection reconcile failed (ignored): %s", exc)
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _reembed_rows(client, name: str, cfg: dict) -> Optional[list]:
    """Fetch every stored payload and embed its text with the new embedder.

    Every embedding must succeed *before* the caller drops the old collection —
    a drop after a half-done migration would destroy memories whenever the new
    model happens to be unreachable.
    """
    emb = cfg["embedder"]["config"]
    try:
        from openai import OpenAI
        from core.services.desktop_model_credentials import sync_client_kwargs

        oai = OpenAI(
            api_key=emb["api_key"],
            base_url=emb["openai_base_url"],
            **sync_client_kwargs(emb["api_key"], emb["openai_base_url"], timeout=30),
            timeout=30,
            max_retries=1,
        )
        out = []
        offset = 0
        page = 500
        while True:
            batch = client.query(
                collection_name=name,
                filter='id != ""',
                output_fields=["id", "metadata"],
                offset=offset,
                limit=page,
            )
            if not batch:
                break
            for row in batch:
                payload = row.get("metadata") or {}
                text = payload.get("data")
                if not text:
                    continue  # e.g. malformed legacy rows; nothing to re-embed
                vector = oai.embeddings.create(input=[text], model=emb["model"]).data[0].embedding
                out.append({"id": row.get("id"), "payload": payload, "vector": vector})
            if len(batch) < page:
                break
            offset += page
        return out
    except Exception as exc:
        logger.error("[MemoryService] re-embedding for migration failed: %s", exc)
        return None


def _replay_migrated_rows(instance, rows: list) -> None:
    """Insert re-embedded rows into the collection mem0 just recreated."""
    try:
        instance.vector_store.insert(
            ids=[row["id"] for row in rows],
            vectors=[row["vector"] for row in rows],
            payloads=[row["payload"] for row in rows],
        )
        logger.info("[MemoryService] migration replay done: %d memories restored", len(rows))
    except Exception as exc:
        logger.error("[MemoryService] migration replay failed (%d rows): %s", len(rows), exc)


def _desktop_memory_epoch() -> str:
    """Nonsecret identity for clients that hold desktop authorization hooks."""
    from core.auth.desktop_bridge import bridge_enabled
    from core.capabilities.paths import capabilities_enabled

    if not bridge_enabled() or not capabilities_enabled():
        return ""
    from core.services.desktop_cloud_bridge import _state_fingerprint, get_state

    return _state_fingerprint(get_state())


def _get_memory() -> Optional[object]:
    """Thread-safe lazy initialization: cache the instance on success; on failure allow retry next time."""
    global _memory_instance, _memory_init_failed, _memory_epoch

    if not settings.memory.enabled:
        return None

    epoch = _desktop_memory_epoch()
    # Existing callers retain their captured hooks; only new callers rebuild.
    if _memory_instance is not None and _memory_epoch == epoch:
        return _memory_instance

    with _memory_lock:
        # Double-check after acquiring lock
        epoch = _desktop_memory_epoch()
        if _memory_instance is not None and _memory_epoch == epoch:
            return _memory_instance
        _memory_instance = None
        _memory_epoch = None

        try:
            _patch_mem0_embedding()
            from mem0 import Memory

            cfg = _build_mem0_config()
            _apply_probed_embed_dims(cfg)
            replay_rows = _reconcile_vector_collection(cfg)
            logger.info(
                "[MemoryService] 初始化 mem0.Memory (graph=%s)", settings.memory.graph_enabled
            )
            instance = Memory.from_config(cfg)
            from core.services.desktop_model_credentials import is_reference, sync_client_kwargs
            from openai import OpenAI

            for component, section in (
                (instance.llm, "llm"),
                (instance.embedding_model, "embedder"),
            ):
                entry = cfg[section]["config"]
                if is_reference(entry["api_key"]):
                    old_client = component.client
                    component.client = OpenAI(
                        api_key=entry["api_key"],
                        base_url=entry["openai_base_url"],
                        max_retries=0,
                        **sync_client_kwargs(entry["api_key"], entry["openai_base_url"]),
                    )
                    old_client.close()
            if epoch != _desktop_memory_epoch():
                from core.capabilities.errors import CloudUnavailable

                raise CloudUnavailable("cloud account changed while preparing memory")
            if replay_rows:
                # Embedding-model switch: refill the freshly created collection
                # with the rows re-embedded during reconciliation.
                _replay_migrated_rows(instance, replay_rows)
            if epoch != _desktop_memory_epoch():
                from core.capabilities.errors import CloudUnavailable

                raise CloudUnavailable("cloud account changed while preparing memory")
            _memory_instance = instance
            _memory_epoch = epoch
            _memory_init_failed = False
            return _memory_instance
        except Exception as exc:
            _memory_init_failed = True
            logger.error("[MemoryService] 初始化失败，记忆功能将降级为空: %s", exc)
            return None


def _reset_memory() -> None:
    """Reset the cached memory instance so that the next call to _get_memory() reinitializes it.

    Used when the Milvus connection is broken (e.g. closed channel).
    """
    global _memory_instance, _memory_init_failed, _memory_epoch
    with _memory_lock:
        _memory_instance = None
        _memory_epoch = None
        _memory_init_failed = False
    logger.info("[MemoryService] 已重置 mem0 实例，下次调用将重新初始化")


def reset_runtime() -> None:
    """Invalidation entry point for model-config changes (called by the config routes).

    The mem0 singleton is built from the model config *as of init time*; clearing
    the ModelConfigService cache alone never rebuilds an existing instance — if the
    first init ran with the placeholder key, every later write keeps failing 401.
    """
    _reset_memory()


def _is_connection_error(exc: Exception) -> bool:
    """Check if an exception indicates a broken Milvus/gRPC connection."""
    msg = str(exc).lower()
    return any(kw in msg for kw in ("closed channel", "connection refused", "unavailable", "grpc"))


def _is_auth_error(exc: Exception) -> bool:
    """Credential-style failures: the instance was most likely built from a stale or
    placeholder config, so a reset followed by a rebuild against the current config
    self-heals. Kept separate from `_is_connection_error` on purpose — auth errors
    must not count against the Milvus circuit breaker.
    """
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "invalid_api_key",
            "incorrect api key",
            "unauthorized",
            "error code: 401",
            "401 unauthorized",
        )
    )
