"""Per-conversation team work copies; source writes use revision checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex

from fastapi import HTTPException

MANIFEST = ".hugagent-source-manifest.json"


def directory(project_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id):
        raise HTTPException(400, "非法项目编号")
    return "/workspace/projects/" + project_id


def snapshot(scope, actor):
    from core.db.engine import SessionLocal
    from core.services.project_source import ProjectSourceService

    from .project_source_access import validate_scope

    with SessionLocal() as db:
        validate_scope(db, scope, actor, write=False)
        return ProjectSourceService(db).snapshot(scope.project_id, actor)


async def prepare(provider, session, scope, actor):
    before = await asyncio.to_thread(snapshot, scope, actor)
    root = directory(scope.project_id)
    from core.sandbox import ExecuteRequest, SandboxError

    previous = []
    try:
        previous = json.loads(
            (await provider.get_file(session, root + "/" + MANIFEST, user_id=actor)).decode()
        )
    except (FileNotFoundError, SandboxError):
        pass
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(409, "项目工作副本清单损坏，请新建项目会话")
    if not isinstance(previous, list) or any(
        not isinstance(path, str)
        or not path
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        for path in previous
    ):
        raise HTTPException(409, "项目工作副本清单包含非法路径")
    current = {path for path, _ in before}
    obsolete = set(previous) - current
    if obsolete:
        result = await provider.execute(
            ExecuteRequest(
                script_content="rm -f -- "
                + " ".join(shlex.quote(root + "/" + path) for path in obsolete),
                script_name="_project_cleanup.sh",
                language="bash",
                timeout=30,
                session_id=session,
                user_id=actor,
            )
        )
        if result.exit_code:
            raise HTTPException(409, "无法清理过期项目工作副本")
    for path, data in before:
        if path == MANIFEST:
            raise HTTPException(409, "项目文件名与工作副本清单冲突")
        await provider.put_file(session, root + "/" + path, data, user_id=actor)
    await provider.put_file(
        session, root + "/" + MANIFEST, json.dumps(sorted(current)).encode(), user_id=actor
    )
    return before


async def persist(session, scope, actor, before):
    from core.services.site_packaging import pack_and_fetch_dir

    files, error = await pack_and_fetch_dir(
        directory(scope.project_id),
        session,
        actor,
        extra_excludes=("dist", ".vite", "*.log", MANIFEST),
    )
    if error:
        raise HTTPException(409, "命令已执行，但源码未保存：" + error)
    baseline = {path: hashlib.sha256(data).hexdigest() for path, data in before}
    changes = [
        (path, data)
        for path, data in files
        if baseline.get(path) != hashlib.sha256(data).hexdigest()
    ]
    if not changes:
        return 0

    def save():
        from core.db.engine import SessionLocal
        from core.services.project_source import ProjectSourceService

        from .project_source_access import validate_scope

        with SessionLocal() as db:
            validate_scope(db, scope, actor, write=True)
            return ProjectSourceService(db).write_files(
                scope.project_id,
                actor,
                changes,
                revisions={path: baseline.get(path, "") for path, _ in changes},
            )

    await asyncio.to_thread(save)
    from core.sandbox import get_sandbox_provider

    paths = sorted(set(baseline) | {path for path, _ in files})
    await get_sandbox_provider().put_file(
        session,
        directory(scope.project_id) + "/" + MANIFEST,
        json.dumps(paths).encode(),
        user_id=actor,
    )
    return len(changes)
