"""Opt-in local E2E: real HTTP, PostgreSQL, storage, tools and OpenSandbox."""
import asyncio
import json
import shutil
import uuid

import httpx

from core.db.engine import SessionLocal
from core.db.models import Artifact, ChatSession, UserShadow
from core.infra.logging import LogContext
from core.llm.tools.myspace_tool import register_myspace_tools
from core.llm.tools.sandbox_tool import register_bash
from core.sandbox._common import myspace_cache_dir
from core.services.api_key_service import ApiKeyService
from core.storage import get_storage


class Toolkit:
    def __init__(self):
        self.fns = {}

    def register_tool_function(self, fn, **kwargs):
        self.fns[fn.__name__] = fn

    def remove_tool_function(self, name):
        self.fns.pop(name, None)


def decode(response):
    return json.loads(response.content[0].text)


async def main():
    uid = "e2e_myspace_" + uuid.uuid4().hex[:12]
    chat = uid + "_chat"
    with SessionLocal() as db:
        db.add(UserShadow(user_id=uid, username=uid, extra_data={'can_use_api_key': True}))
        db.commit()
        db.add(ChatSession(chat_id=chat, user_id=uid, title="MySpace identity E2E"))
        db.commit()
        _, raw = ApiKeyService(db).create_key(uid, "temporary E2E", expires_in_days=1)
    print("E2E_USER=" + uid, flush=True)
    tk = Toolkit()
    register_bash(tk, loader=None, loaded_skill_ids=set(), chat_id=chat,
                  sandbox_session_id=chat, user_id=uid)
    register_myspace_tools(tk, user_id=uid, scope=None)
    checks = 0

    def check(label, condition):
        nonlocal checks
        assert condition, label
        checks += 1
        print("PASS " + label, flush=True)

    async def bash(command):
        result = decode(await tk.fns["bash"](command, yield_time_ms=10000))
        while result.get("status") == "running":
            result = decode(await tk.fns["write_stdin"](result["session_id"], yield_time_ms=10000))
        assert not result.get("error") and result.get("exit_code") == 0, result
        return result.get("stdout", "")

    try:
        with LogContext(user_id=uid, chat_id=chat):
            async with httpx.AsyncClient(base_url="http://127.0.0.1:3001",
                                         headers={"Authorization": "Bearer " + raw},
                                         timeout=90) as client:
                async def upload(name, body):
                    return await client.post("/v1/file/upload", files={"file": (name, body)})

                async def listing():
                    for attempt in range(50):
                        response = await client.get("/v1/artifacts", params={"scope": "personal", "page_size": 1000})
                        if response.status_code != 409:
                            break
                        await asyncio.sleep(.1)
                    assert response.status_code == 200, response.text[:500]
                    return response.json()["data"]["items"]

                first = await upload("same.txt", b"one")
                check(f"HTTP upload ({first.status_code}, {first.text[:250] if first.status_code != 200 else 'ok'})", first.status_code == 200)
                ident = first.json()["file_id"]
                check("duplicate HTTP upload is 409", (await upload("same.txt", b"two")).status_code == 409)
                race = await asyncio.gather(*(upload("race.txt", str(n).encode()) for n in range(5)))
                check("five concurrent uploads have one winner", sorted(r.status_code for r in race) == [200, 409, 409, 409, 409])
                changed = await client.put("/v1/file/" + ident, files={"file": ("same.txt", b"edited")})
                check("HTTP edit retains ID", changed.status_code == 200 and changed.json()["file_id"] == ident)
                await listing()
                check("sandbox sees edited bytes", await bash("cat /myspace/same.txt") == "edited")
                await bash("printf rapid > /myspace/same.txt; printf created > /myspace/from-bash.txt")
                # The observer receives asynchronous filesystem events. Poll the public read barrier.
                for _ in range(100):
                    await asyncio.sleep(0.1)
                    items = await listing()
                    with SessionLocal() as db:
                        art = db.get(Artifact, ident)
                        if get_storage().download_bytes(art.storage_key) == b"rapid" and any(i["name"] == "from-bash.txt" for i in items):
                            break
                check("sandbox rapid overwrite updates original ID", get_storage().download_bytes(art.storage_key) == b"rapid")
                check("sandbox new file appears in HTTP", any(i["name"] == "from-bash.txt" for i in items))
                tool = decode(await tk.fns["space_list_myspace_files"](limit=1000))
                names = {row.get("name") or row.get("filename") for row in tool.get("items", [])}
                check("tool list equals HTTP list", names == {i["name"] for i in items})
                disk = set((await bash("find /myspace/ -type f -printf '%f\\n'")).splitlines())
                check("sandbox path set equals HTTP list", disk == {i["name"] for i in items})
                with SessionLocal() as db:
                    rows = db.query(Artifact).filter(Artifact.user_id == uid, Artifact.deleted_at.is_(None)).all()
                    check("all sandbox bytes equal database storage", all(
                        (myspace_cache_dir(uid) / row.filename).read_bytes() == get_storage().download_bytes(row.storage_key)
                        for row in rows
                    ))
                deleted = await client.delete("/v1/artifacts/" + ident)
                check("HTTP delete", deleted.status_code == 200)
                await listing()
                check("delete disappears from sandbox", (await bash("test ! -f /myspace/same.txt && echo absent")).strip() == "absent")
                recreated = await upload("same.txt", b"new identity")
                check("name reusable after deletion", recreated.status_code == 200 and recreated.json()["file_id"] != ident)
                await listing()
                check("old tombstone cannot remove recreated file", await bash("cat /myspace/same.txt") == "new identity")
        print(f"E2E_RESULT={checks}/{checks}", flush=True)
    finally:
        # Only this run's isolated user and files are touched.
        from core.sandbox import get_sandbox_provider
        await get_sandbox_provider().close_session(chat)
        with SessionLocal() as db:
            user = db.get(UserShadow, uid)
            if user:
                db.delete(user)
                db.commit()
        root = myspace_cache_dir(uid).resolve()
        assert root.name == uid and uid.startswith("e2e_myspace_")
        shutil.rmtree(root, ignore_errors=True)
        print("E2E temporary identity removed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
