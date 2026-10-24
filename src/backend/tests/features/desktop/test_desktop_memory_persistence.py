"""The application's real mem0 backend retains memory across process restart."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def test_real_memory_backend_preserves_ids_payloads_and_user_isolation_on_restart(tmp_path):
    requests = []

    class Embeddings(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/embeddings"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            texts = body["input"]
            if isinstance(texts, str):
                texts = [texts]
            vectors = []
            for index, text in enumerate(texts):
                raw = hashlib.sha256(text.encode()).digest()[:8]
                norm = sum((v + 1) ** 2 for v in raw) ** 0.5
                vectors.append(
                    dict(object="embedding", index=index, embedding=[(v + 1) / norm for v in raw])
                )
            data = json.dumps(
                dict(
                    object="list",
                    data=vectors,
                    model=body["model"],
                    usage=dict(prompt_tokens=len(texts), total_tokens=len(texts)),
                )
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Embeddings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Separate processes guarantee the second initialization cannot use an
    # in-memory mem0/Milvus cache. Both point only to the temporary data directory.
    program = r"""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

root, url, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
from core.memory import backend
from core.models.config import ModelConfigService

memory_settings = SimpleNamespace(
    enabled=True, graph_enabled=False,
    model_name="isolated-model", model_url=url, api_key="isolated-test-key",
    embed_model="isolated-embedding", embed_url=url, embed_api_key="isolated-test-key",
    embed_dims=8, milvus_url=str(root / "memories.db"), milvus_token="",
)
backend.settings = SimpleNamespace(memory=memory_settings)
with patch.object(ModelConfigService, "get_instance",
                  return_value=SimpleNamespace(resolve=lambda role: None)):
    if phase == "load-failure":
        from pymilvus import MilvusClient, connections
        connected = {alias for alias, handler in connections.list_connections() if handler}
        with patch.object(MilvusClient, "load_collection",
                          side_effect=RuntimeError("injected collection load failure")):
            for attempt in range(2):
                assert backend._get_memory() is None
                assert backend.process_state().memory_init_failed
                assert backend.process_state().memory_instance is None
                assert {alias for alias, handler in connections.list_connections() if handler} == connected
        memory = backend._get_memory()
        assert memory is not None
        saved = json.loads((root / "saved-ids.json").read_text())
        assert memory.get(saved["ids"]["alice"]) == saved["original"]
        memory.vector_store.client.close()
        memory.embedding_model.client.close()
        memory.llm.client.close()
        memory.close()
        raise SystemExit(0)
    memory = backend._get_memory()
assert memory is not None, "actual application memory initialization failed"
assert type(memory).__module__ == "mem0.memory.main"
ids_file = root / "saved-ids.json"
try:
    if phase == "seed":
        alice = memory.add("Alice preserves this procedure.", user_id="alice",
                           metadata={"source": "original", "custom": {"revision": 1}}, infer=False)
        bob = memory.add("Bob retains a private procedure.", user_id="bob",
                         metadata={"source": "private"}, infer=False)
        ids = {"alice": alice["results"][0]["id"], "bob": bob["results"][0]["id"]}
        original = memory.get(ids["alice"])
        assert original["metadata"]["custom"] == {"revision": 1}
        ids_file.write_text(json.dumps({"ids": ids, "original": original}))
    else:
        saved = json.loads(ids_file.read_text())
        ids, original = saved["ids"], saved["original"]
        restored = memory.get(ids["alice"])
        assert restored == original, (restored, original)
        alice_records = memory.get_all(filters={"user_id": "alice"})["results"]
        bob_records = memory.get_all(filters={"user_id": "bob"})["results"]
        assert [r["id"] for r in alice_records] == [ids["alice"]]
        assert [r["id"] for r in bob_records] == [ids["bob"]]
        added = memory.add("Alice appends a second procedure.", user_id="alice",
                           metadata={"source": "appended"}, infer=False)
        new_id = added["results"][0]["id"]
        assert new_id != ids["alice"]
        assert memory.get(ids["alice"]) == original
        assert {r["id"] for r in memory.get_all(filters={"user_id": "alice"})["results"]} == {
            ids["alice"], new_id
        }
        assert [r["id"] for r in memory.get_all(filters={"user_id": "bob"})["results"]] == [
            ids["bob"]
        ]
        found = memory.search("Alice preserves this procedure.",
                              filters={"user_id": "alice"})["results"]
        assert found and all(row["user_id"] == "alice" for row in found)
        assert ids["alice"] in {r["id"] for r in found}
finally:
    memory.vector_store.client.close()
    memory.embedding_model.client.close()
    memory.llm.client.close()
    memory.close()
"""
    backend_path = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(backend_path), os.getenv("PYTHONPATH")))),
        "HOME": str(tmp_path),
        "MEM0_DIR": str(tmp_path / "mem0"),
        "MEM0_TELEMETRY": "false",
        "DO_NOT_TRACK": "1",
    }
    env["NO_PROXY"] = "127.0.0.1,localhost"
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1"
        for phase in ("seed", "load-failure", "reopen"):
            completed = subprocess.run(
                [sys.executable, "-c", program, str(tmp_path), url, phase],
                cwd=tmp_path,
                env=env,
                text=True,
                capture_output=True,
                timeout=90,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
        assert any("dimension probe" in item["input"] for item in requests)
        assert all(item["model"] == "isolated-embedding" for item in requests)
        assert (tmp_path / "memories.db").exists()
        assert (tmp_path / "mem0" / "history.db").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
