"""真实端到端：把各类工具挨个对「我的空间」跑一遍（真 DB + 真对象存储 + 真 opensandbox）。

覆盖 CreateFolder / Write / Read / Edit / Glob / Grep / list_myspace_files / bash /
Move / Delete / sandbox_get_artifact，并回查数据库真值。所有对象用 __tt__ 前缀、结尾硬清理。

pytest 不收集（文件名以 e2e_ 开头），在 backend 容器里手动跑：
    docker exec -e E2E_USER_ID=<真实用户号> hugagent-backend \
        python src/backend/tests/sandbox/e2e_myspace_tools_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

USER_ID = os.getenv("E2E_USER_ID", "")
if not USER_ID:
    raise SystemExit("请用 E2E_USER_ID=<目标环境里的真实用户号> 运行本脚本")
TAG = f"__tt__{uuid.uuid4().hex[:6]}"
FOLDER = f"测试目录_{TAG}"
SENTINEL = f"SENT{uuid.uuid4().hex[:8].upper()}"

results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


class RecordingToolkit:
    """只记录被注册的工具函数，按名字取回来直接调用。"""

    def __init__(self):
        self.fns = {}

    def register_tool_function(self, fn, **kw):
        self.fns[fn.__name__] = fn

    def remove_tool_function(self, name):
        self.fns.pop(name, None)


def text_of(resp):
    try:
        return resp.content[0].text
    except Exception:
        return str(resp)


def js(resp):
    t = text_of(resp)
    try:
        return json.loads(t)
    except Exception:
        return {"_raw": t}


async def main():
    from core.llm.tools._state import ReadStateTracker
    from core.llm.tools.edit_tool import register_edit
    from core.llm.tools.fileops_tool import register_delete, register_mkdir, register_move
    from core.llm.tools.glob_tool import register_glob
    from core.llm.tools.grep_tool import register_grep
    from core.llm.tools.myspace_tool import register_myspace_tools
    from core.llm.tools.read_tool import register_read
    from core.llm.tools.sandbox_tool import register_bash, register_sandbox_get_artifact
    from core.llm.tools.write_tool import register_write

    chat_id = f"chat_tt_{uuid.uuid4().hex[:8]}"

    # 真实请求路径由 orchestration/streaming.py 的 LogContext 设置 user_id_var，
    # 沙箱 put_file/get_file 这些"不带 user_id 的旧调用方"靠它回填用户身份。
    # 脱离 HTTP 直接调工具必须自己补上，否则沙箱会以匿名身份创建（没有 myspace
    # 挂载、也没有 /myspace 软链），测出来的失败是测试脚手架的问题而不是产品问题。
    from core.infra.logging import LogContext

    # 建一条真的会话行：artifacts.chat_id 有外键，缺了会退化成"不挂会话登记"。
    from core.db.engine import SessionLocal as _SL
    from core.db.models import ChatSession as _CS

    _db = _SL()
    try:
        _db.add(_CS(chat_id=chat_id, user_id=USER_ID, title="__tt__ e2e"))
        _db.commit()
    finally:
        _db.close()

    tk = RecordingToolkit()
    st = ReadStateTracker()
    common = dict(
        chat_id=chat_id,
        sandbox_session_id=chat_id,
        user_id=USER_ID,
        project_folder_name=None,
        scope=None,
    )
    register_read(tk, state=st, **common)
    register_write(tk, state=st, interactive=False, **common)
    register_edit(tk, state=st, interactive=False, **common)
    register_glob(tk, **common)
    register_grep(tk, **common)
    register_delete(tk, state=st, interactive=False, **common)
    register_move(tk, state=st, interactive=False, **common)
    register_mkdir(tk, interactive=False, **common)
    register_myspace_tools(tk, user_id=USER_ID, scope=None)
    register_bash(
        tk,
        loader=None,
        loaded_skill_ids=set(),
        chat_id=chat_id,
        sandbox_session_id=chat_id,
        user_id=USER_ID,
        interactive=False,
    )
    register_sandbox_get_artifact(
        tk, chat_id=chat_id, sandbox_session_id=chat_id, user_id=USER_ID
    )
    print("已注册工具:", ", ".join(sorted(tk.fns)), flush=True)

    ctx = LogContext(user_id=USER_ID, chat_id=chat_id)
    ctx.__enter__()

    F = tk.fns

    def p(*a):
        return "/myspace/" + "/".join(a)

    note = f"笔记_{TAG}.txt"

    # 1. CreateFolder
    r = js(await F["CreateFolder"](p(FOLDER)))
    check("CreateFolder 建目录", not r.get("error") and not r.get("blocked"), str(r)[:160])

    # 2. Write
    body = f"第一行\n负责人：张伟\n校验码 {SENTINEL}\n最后一行\n"
    r = js(await F["Write"](p(FOLDER, note), body))
    check("Write 写入我的空间", not r.get("error") and not r.get("blocked"), str(r)[:200])

    # 3. list_myspace_files
    r = js(await F["list_myspace_files"](limit=100))
    subs = [f.get("name") for f in (r.get("sub_folders") or [])]
    check("list_myspace_files 看到新建目录", FOLDER in subs, f"共 {len(subs)} 个子目录")

    # 4. Read
    r = js(await F["Read"](p(FOLDER, note)))
    content = r.get("content") or r.get("text") or str(r)
    check("Read 读回内容", SENTINEL in content, content[:120].replace("\n", "\\n"))

    # 5. Edit
    r = js(await F["Edit"](p(FOLDER, note), "负责人：张伟", "负责人：李娜"))
    r2 = js(await F["Read"](p(FOLDER, note)))
    c2 = r2.get("content") or r2.get("text") or str(r2)
    check("Edit 精确替换生效", "李娜" in c2 and "张伟" not in c2, str(r)[:140])

    # 6. Glob
    r = js(await F["Glob"]("**/*.txt", p(FOLDER)))
    files = r.get("filenames") or r.get("files") or r.get("matches") or []
    check("Glob 匹配到我的空间文件", any(TAG in str(f) for f in files), str(files)[:160])

    # 7. Grep
    r = js(await F["Grep"](SENTINEL, p(FOLDER)))
    check("Grep 搜到校验码", (r.get("num_matches") or 0) > 0 or SENTINEL in str(r), str(r)[:200])

    # 8. bash 写 /myspace 自动登记
    fn = f"脚本产出_{TAG}.txt"
    r = js(await F["bash"](f"echo '{SENTINEL}-from-bash' > '/myspace/{FOLDER}/{fn}'"))
    synced = r.get("myspace_synced") or []
    check(
        "bash 写 /myspace 自动登记",
        r.get("myspace_synced_count", 0) >= 1,
        f"exit={r.get('exit_code')} stderr={str(r.get('stderr'))[:200]} synced={[s.get('name') for s in synced]}",
    )

    # 9. bash 读到工具写的文件（同一份）
    r = js(await F["bash"](f"cat '/myspace/{FOLDER}/{note}'"))
    check("bash 读到工具写的文件", "李娜" in str(r.get("stdout", "")), str(r.get("stdout"))[:120])

    # 10. Move
    renamed = f"改名后_{TAG}.txt"
    r = js(await F["Move"](p(FOLDER, fn), p(FOLDER, renamed)))
    check("Move 改名", not r.get("error") and not r.get("blocked"), str(r)[:160])
    r = js(await F["Read"](p(FOLDER, renamed)))
    c = r.get("content") or r.get("text") or str(r)
    check("Move 后仍可读", "from-bash" in c, c[:100])

    # 11. Delete
    r = js(await F["Delete"](p(FOLDER, renamed)))
    check("Delete 删除文件", not r.get("error") and not r.get("blocked"), str(r)[:160])
    r = js(await F["Read"](p(FOLDER, renamed)))
    gone = bool(r.get("error")) or "不存在" in str(r) or "not found" in str(r).lower()
    check("Delete 后读不到", gone, str(r)[:120])

    # 12. sandbox_get_artifact
    await F["bash"](f"echo 'artifact-{SENTINEL}' > '/workspace/scratch/产物_{TAG}.txt'")
    r = js(await F["sandbox_get_artifact"](f"/workspace/scratch/产物_{TAG}.txt"))
    fid = r.get("file_id") or r.get("artifact_id")
    check("sandbox_get_artifact 登记沙箱产物", bool(fid), str(r)[:160])

    # 13. 落库真值核对
    from core.db.engine import SessionLocal
    from core.db.models import Artifact, UserFolder

    db = SessionLocal()
    try:
        folder = (
            db.query(UserFolder)
            .filter(
                UserFolder.user_id == USER_ID,
                UserFolder.name == FOLDER,
                UserFolder.deleted_at.is_(None),
            )
            .first()
        )
        check("DB 里文件夹记录存在", folder is not None, getattr(folder, "folder_id", ""))
        arts = (
            db.query(Artifact)
            .filter(Artifact.user_id == USER_ID, Artifact.filename.like(f"%{TAG}%"))
            .all()
        )
        alive = [a for a in arts if a.deleted_at is None]
        dead = [a for a in arts if a.deleted_at is not None]
        check(
            "DB 里存活/软删条数合理",
            len(alive) >= 1 and len(dead) >= 1,
            f"alive={[a.filename for a in alive]} deleted={[a.filename for a in dead]}",
        )
    finally:
        db.close()

    # ── 清理 ──
    try:
        await F["Delete"](p(FOLDER))
    except Exception as exc:
        print("清理 Delete 失败:", exc)
    db = SessionLocal()
    try:
        for a in (
            db.query(Artifact)
            .filter(Artifact.user_id == USER_ID, Artifact.filename.like(f"%{TAG}%"))
            .all()
        ):
            db.delete(a)
        for f in db.query(UserFolder).filter(
            UserFolder.user_id == USER_ID, UserFolder.name == FOLDER
        ).all():
            db.delete(f)
        for c in db.query(_CS).filter(_CS.chat_id == chat_id).all():
            db.delete(c)
        db.commit()
    finally:
        db.close()
    try:
        await F["bash"](f"rm -rf '/myspace/{FOLDER}' '/workspace/scratch/产物_{TAG}.txt'")
    except Exception:
        pass

    print("\n" + "=" * 60)
    ok = sum(1 for _, c, _ in results if c)
    print(f"合计 {ok}/{len(results)} 通过")
    for n, c, d in results:
        if not c:
            print(f"  失败: {n} — {d}")
    sys.exit(0 if ok == len(results) else 1)


asyncio.run(main())
