"""Opt-in 600-file regression against real HTTP, Redis, DB and OpenSandbox."""
import asyncio
import json
import shutil
import time
import uuid
import httpx
from core.db.engine import SessionLocal
from core.db.models import Artifact, ChatSession, UserShadow
from core.infra.logging import LogContext
from core.llm.tools.sandbox_tool import register_bash
from core.sandbox._common import myspace_cache_dir
from core.services.api_key_service import ApiKeyService
from core.storage import get_storage
from tests.sandbox.e2e_myspace_identity_live import Toolkit, decode


async def main():
    uid = "e2e_myspace_bulk_" + uuid.uuid4().hex[:10]
    chat = uid + "_chat"
    with SessionLocal() as db:
        db.add(UserShadow(user_id=uid, username=uid, extra_data={"can_use_api_key": True}))
        db.commit()
        db.add(ChatSession(chat_id=chat, user_id=uid, title="600 file MySpace regression"))
        db.commit()
        _, raw = ApiKeyService(db).create_key(uid, "temporary bulk E2E", expires_in_days=1)
    tk = Toolkit()
    register_bash(tk, loader=None, loaded_skill_ids=set(), chat_id=chat,
                  sandbox_session_id=chat, user_id=uid)
    metrics = {"user": uid}

    async def bash(command):
        response = decode(await tk.fns["bash"](command, yield_time_ms=10000))
        while response.get("status") == "running":
            response = decode(await tk.fns["write_stdin"](response["session_id"], yield_time_ms=10000))
        assert response.get("exit_code") == 0 and not response.get("error"), response

    try:
        with LogContext(user_id=uid, chat_id=chat):
            async with httpx.AsyncClient(base_url="http://127.0.0.1:3001",
                    headers={"Authorization": "Bearer " + raw}, timeout=90) as client:
                async def listing():
                    started = time.monotonic()
                    response = await client.get("/v1/artifacts", params={"scope": "personal", "page_size": 1000})
                    elapsed = round(time.monotonic()-started, 3)
                    assert response.status_code == 200, (response.status_code, elapsed, response.text[:150])
                    return response.json()["data"]["items"], elapsed

                await bash("for i in $(seq 1 600); do printf 'version-one' > /myspace/bulk_$i.txt; done")
                for attempt in range(100):
                    items, elapsed = await listing()
                    if len(items) == 600:
                        break
                    await asyncio.sleep(0.1)
                assert len(items) == 600, len(items)
                metrics["bulk_create_seconds"] = elapsed
                print("PASS real sandbox creates 600 files; HTTP returns all", flush=True)
                baseline = {i["name"]: i["file_id"] for i in items}
                for round_no in range(3):
                    await bash("for f in /myspace/bulk_*.txt; do cat \"$f\" > /dev/null; done")
                    results = await asyncio.gather(*(listing() for _ in range(10)))
                    assert all(len(rows) == 600 for rows, _ in results)
                    print(f"PASS read storm round {round_no+1}: 10 concurrent HTTP reads, max {max(t for _,t in results):.3f}s", flush=True)
                await asyncio.gather(
                    bash("for i in $(seq 1 30); do printf 'version-two' > /myspace/bulk_$i.txt; done"),
                    *(listing() for _ in range(10)),
                )
                print("PASS 10 concurrent reads during sandbox writes", flush=True)
                for attempt in range(100):
                    items, _ = await listing()
                    with SessionLocal() as db:
                        rows = db.query(Artifact).filter(Artifact.user_id == uid, Artifact.deleted_at.is_(None)).all()
                        matching = all(get_storage().download_bytes(row.storage_key) ==
                            (b"version-two" if int(row.filename[5:-4]) <= 30 else b"version-one") for row in rows)
                    if matching: break
                    await asyncio.sleep(0.1)
                assert matching
                assert {i["name"]:i["file_id"] for i in items} == baseline
                print("PASS 30 rapid sandbox edits retain all 600 IDs and exact bytes", flush=True)
                responses = await asyncio.gather(*(client.post("/v1/file/upload",
                    files={"file": ("race.txt", str(n).encode())}) for n in range(10)))
                assert sorted(r.status_code for r in responses) == [200] + [409]*9
                print("PASS 10 concurrent duplicate uploads: one winner", flush=True)
                items, _ = await listing()
                paths = {p.name for p in myspace_cache_dir(uid).iterdir() if p.is_file()}
                assert paths == {i["name"] for i in items}
                print("BULK_E2E_PASS files=601 parallel_reads=40 edits=30 duplicate_uploads=10", flush=True)
    finally:
        from core.sandbox import get_sandbox_provider
        await get_sandbox_provider().close_session(chat)
        with SessionLocal() as db:
            keys = [r[0] for r in db.query(Artifact.storage_key).filter(Artifact.user_id == uid)]
            user = db.get(UserShadow, uid)
            if user:
                db.delete(user)
                db.commit()
        for key in keys:
            get_storage().delete(key)
        root = myspace_cache_dir(uid).resolve()
        assert root.name == uid and uid.startswith("e2e_myspace_bulk_")
        shutil.rmtree(root, ignore_errors=True)
        print("Bulk E2E temporary identity removed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
