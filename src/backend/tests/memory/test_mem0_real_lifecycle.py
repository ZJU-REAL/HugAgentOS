"""Public L2 lifecycle against genuine mem0, Milvus Lite and embedding HTTP."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.infra.time import utc_now
from core.memory import backend, service
from core.memory.context import MemoryContext


@pytest.fixture
def real_memory(tmp_path, monkeypatch):
    mem0 = pytest.importorskip("mem0")
    pytest.importorskip("milvus_lite")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(self.path)
            if self.path == "/v1/embeddings":
                texts = body["input"]
                if isinstance(texts, str):
                    texts = [texts]
                vectors = []
                for text in texts:
                    vector = [0.0] * 16
                    for word in str(text).split():
                        index = hashlib.sha256(word.encode()).digest()[0] % 16
                        vector[index] += 1
                    norm = math.sqrt(sum(value * value for value in vector)) or 1
                    vectors.append([value / norm for value in vector])
                result = {"object": "list", "model": "fixture", "data": [
                    {"object": "embedding", "index": index, "embedding": vector}
                    for index, vector in enumerate(vectors)
                ], "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            else:
                result = {"id": "fixture", "object": "chat.completion", "choices": [
                    {"index": 0, "message": {"role": "assistant", "content":
                     '{"entities": [], "facts": []}'}, "finish_reason": "stop"}
                ], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            encoded = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    config = {
        "history_db_path": str(tmp_path / "history.sqlite"),
        "llm": {"provider": "openai", "config": {
            "model": "fixture", "openai_base_url": url, "api_key": "fixture-only"}},
        "embedder": {"provider": "openai", "config": {
            "model": "fixture", "openai_base_url": url, "api_key": "fixture-only"}},
        "vector_store": {"provider": "milvus", "config": {
            # Match production: mem0 validates token as str, even for local no-auth stores.
            "token": "", "url": str(tmp_path / "vectors.db"), "collection_name": "l2_lifecycle",
            "embedding_model_dims": 16, "metric_type": "COSINE"}},
    }
    memory = None
    try:
        backend._patch_mem0_embedding()
        memory = mem0.Memory.from_config(config)
        monkeypatch.setattr(backend, "_get_memory", lambda: memory)
        monkeypatch.setattr(backend, "settings", SimpleNamespace(
            memory=replace(backend.settings.memory, enabled=True, graph_enabled=False)))
        from core.memory.pipeline import milvus_breaker
        monkeypatch.setattr(milvus_breaker, "is_open", lambda: False)
        yield memory, requests
    finally:
        if memory is not None:
            memory.vector_store.client.close()
        from milvus_lite.server_manager import server_manager_instance
        server_manager_instance.release_server(str(tmp_path / "vectors.db"))
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def expiry(item):
    # mem0 2.2 promotes expiry to the result root; older releases keep it in metadata.
    return item.get("expiration_date") or (item.get("metadata") or {}).get("expiration_date")


def context(user="owned-user-a", workspace="default"):
    return MemoryContext(user_id=user, workspace_id=workspace, write_enabled=True)


@pytest.mark.asyncio
async def test_public_write_read_recall_scope_edit_delete(real_memory):
    ctx = context()
    text = "Verify source authority before importing the report"
    ident = await service.save_procedure_entry(ctx=ctx, content=text, ttl_days=30,
        memory_meta={"outbox_effect_id": "owned-effect", "strength": "weak"})
    assert ident, "Public L2 write must return a genuine memory ID"
    rows = await service.get_all_memories(ctx.user_id, workspace_id=ctx.workspace_id)
    assert len(rows) == 1 and rows[0]["id"] == ident
    original_meta = rows[0]["metadata"]
    assert expiry(rows[0]) == (utc_now().date() + timedelta(days=30)).isoformat()
    result = await service.retrieve_memories_structured(ctx.user_id, text)
    assert not result.degraded and any(row.memory_id == ident for row in result.items)
    assert not await service.get_all_memories("owned-user-b")
    assert not await service.get_all_memories(ctx.user_id, workspace_id="other-workspace")
    assert not (await service.retrieve_memories_structured("owned-user-b", text)).items
    assert await service.update_memory(ident, "Check source authority before importing", strict=True)
    edited = (await service.get_all_memories(ctx.user_id))[0]
    assert edited["id"] == ident
    assert edited["metadata"] == original_meta
    assert expiry(edited) == expiry(rows[0])
    assert "Check source authority" in edited["memory"]
    assert await service.delete_memory(ident)
    assert not await service.get_all_memories(ctx.user_id)
    assert not (await service.retrieve_memories_structured(ctx.user_id, text)).items
    assert "/v1/embeddings" in real_memory[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [-1, 0])
async def test_expired_rows_hidden_but_effect_receipt_retained(real_memory, days):
    memory, _ = real_memory
    text = "Verify source authority before importing the report"
    memory.add(text, user_id=context().user_id, infer=False, metadata={
        "workspace_id": "default", "layer": "L2", "ttl_days": 1,
        "expiration_date": (utc_now().date() + timedelta(days=days)).isoformat(),
        "outbox_effect_id": "owned-expired-receipt"})
    assert not await service.get_all_memories(context().user_id)
    assert not (await service.retrieve_memories_structured(context().user_id, text)).items
    assert await service.find_similar_procedure(context(), text) is None
    found = await service.find_procedure_by_effect_id(context(), "owned-expired-receipt", strict=True)
    assert found is not None, "Receipt must survive expiry to prevent crash replay duplication"


@pytest.mark.asyncio
async def test_reinforcement_preserves_scope_content_receipts_and_extends_ttl(real_memory):
    ctx = context(workspace="owned-workspace")
    text = "Verify source authority before importing the report"
    ident = await service.save_procedure_entry(ctx=ctx, content=text, ttl_days=7,
        memory_meta={"outbox_effect_id": "job:one", "outbox_effect_ids": ["job:one"],
                     "seen_count": 1, "strength": "weak"})
    assert ident
    similar = await service.find_similar_procedure(ctx, text, min_score=0.4)
    assert similar and similar["id"] == ident
    assert await service.reinforce_procedure_entry(similar, effect_id="job:two",
        candidate_receipts=["job:one"])
    rows = await service.get_all_memories(ctx.user_id, workspace_id=ctx.workspace_id)
    assert len(rows) == 1 and rows[0]["memory"] == text and rows[0]["id"] == ident
    meta = rows[0]["metadata"]
    assert meta["workspace_id"] == ctx.workspace_id and meta["author_user_id"] == ctx.user_id
    assert meta["seen_count"] == 2 and meta["strength"] == "strong"
    assert meta["outbox_effect_ids"] == ["job:one", "job:two"]
    assert expiry(rows[0]) == (
        utc_now().date() + timedelta(days=backend.settings.memory.procedure_ttl_days)).isoformat()
    same = await service.find_similar_procedure(ctx, text, min_score=0.4)
    assert await service.reinforce_procedure_entry(same, effect_id="job:two")
    assert (await service.get_all_memories(ctx.user_id, workspace_id=ctx.workspace_id))[0]["metadata"]["seen_count"] == 2


@pytest.mark.asyncio
async def test_durable_foreign_actor_replay_cannot_modify_owned_memory(
    real_memory, db_session, monkeypatch
):
    from sqlalchemy.orm import sessionmaker
    from core.memory import outbox
    from core.memory.outbox import common
    from core.memory.access import authorized_memory_context

    monkeypatch.setattr(common, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr("core.db.engine.SessionLocal", sessionmaker(bind=db_session.get_bind()))
    ident = await service.save_procedure_entry(ctx=context(), content="Keep original authority")
    assert ident
    assert await authorized_memory_context(ident, context().user_id)
    assert await authorized_memory_context(ident, "owned-user-b") is None
    assert await authorized_memory_context("unknown-id", context().user_id) is None
    job = outbox.enqueue_memory_edit_job(context("owned-user-b"), ident, "foreign rewrite")
    result = await outbox.consume_outbox_job(job)
    assert result["status"] == "retry"  # Existing bounded retry policy; no store effect.
    assert (await service.get_all_memories(context().user_id))[0]["memory"] == "Keep original authority"
