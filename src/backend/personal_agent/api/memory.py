"""Personal Agent memory panel routes: wiki pages, the write log, snapshots and lint.

Owner-scoped like the other personal-agent routes. Page and report text itself is read
and saved through /workspace/file; these routes only describe, revert and restore.
"""

from fastapi import APIRouter, Depends, Query

from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.responses import success_response
from personal_agent.api.dependencies import service
from personal_agent.memory import inject, layout, lint, page, seed, snapshots, sources, state, store, versions
from personal_agent.memory.dreaming.runs import locked as locked_agent
from personal_agent.memory.models import PersonalMemoryVersion
from personal_agent.workspace import files
from personal_agent.workspace.constants import WORKSPACE

router = APIRouter()

DREAMING = "记忆正在整理（Dreaming 运行中），请等它结束后再操作"


def _agent(svc):
    svc.identity.ensure()
    return svc.identity.agent()


def _call(fn):
    """Run fn() and wrap it in the envelope; a backend without the volume reports it."""

    try:
        return success_response(data=fn())
    except files.WorkspaceUnavailable:
        return success_response(data={"available": False})
    except BlockingIOError:
        raise store.MemoryConflict(store.LOCKED) from None


def _workspace_path(rel):
    return WORKSPACE + "/" + rel


def _seed(ident):
    """Make sure _schema.md and _index.md exist; never blocks on a held lock."""
    if store.read(ident, layout.SCHEMA) is not None and store.read(ident, layout.INDEX) is not None: return
    try:
        seed.seed(ident)
    except BlockingIOError:
        pass


def _page_entry(rel, text, pending):
    parsed = page.parse(text)
    name = layout.slug(rel)
    meta = parsed.meta
    return {"path": _workspace_path(rel), "rel": rel, "name": name,
            "type": meta.get("type") or name.split("-", 1)[0],
            "description": meta.get("description") or "", "aliases": parsed.aliases,
            "status": meta.get("status") or "active", "updated": meta.get("updated") or None,
            "size": len(text.encode()), "sources": len(page.sources(text)),
            "pending": name in pending, "moved_to": page.stub_target(text), "links": page.links(text)}


@router.get("/memory/pages")
def memory_pages(svc=Depends(service)):

    agent = _agent(svc)

    def run():
        _seed(agent.id)
        wiki = store.wiki(agent.id)
        index = wiki.get(layout.INDEX, "")
        pending = set(page.links(page.section(index, layout.PENDING_HEADING)))
        pages = [_page_entry(rel, text, pending) for rel, text in wiki.items() if layout.is_page(rel)]
        start = page.section(index, layout.WHERE_TO_START)
        return {"available": True, "pages": pages,
                "index_head": inject.index_head(index),
                "where_to_start": "\n".join(start.splitlines()[1:]).strip(),
                "index_path": _workspace_path(layout.INDEX), "schema_path": _workspace_path(layout.SCHEMA),
                "report_path": _workspace_path(layout.REPORT),
                "types": [{"key": key, "label": layout.TYPE_LABELS[key]} for key in layout.TYPES],
                "busy": _busy(agent)}

    return _call(run)


def _busy(agent):
    return state.busy(agent)


def _rel(path):
    try:
        return files.relative(path)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from None


def _owned_version(db, agent, version_id):
    row = db.get(PersonalMemoryVersion, version_id)
    if row is None or row.agent_id != agent.id: raise ResourceNotFoundError("memory version", version_id)
    return row


def _version(row, current, *, text=False):
    """as_dict plus what the panel needs: the editor path and whether Revert can work now."""
    out = versions.as_dict(row, text=text)
    out["file"] = _workspace_path(row.path)
    out["revertable"] = store.revertable(row, current)
    return out


@router.get("/memory/versions")
def memory_versions(
    path: str | None = Query(None, max_length=512),
    source_id: str | None = Query(None, max_length=160),
    writer: str | None = Query(None, max_length=16),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=100),
    svc=Depends(service),
):

    agent = _agent(svc)
    if writer and writer not in layout.WRITERS: raise BadRequestError("writer 只能是 " + "、".join(layout.WRITERS))
    rel = _rel(path) if path else None

    def run():
        rows = versions.listing(svc.db, agent.id, path=rel, source_id=source_id or None, writer=writer or None,
                                offset=(page - 1) * page_size, limit=page_size + 1, text=False)
        window = rows[:page_size]
        current = {}
        for row in window:
            if row.path not in current: current[row.path] = store.read(agent.id, row.path)
        return {"items": [_version(row, current[row.path]) for row in window], "page": page,
                "page_size": page_size, "has_more": len(rows) > page_size}

    return _call(run)


@router.get("/memory/versions/{version_id}")
def memory_version(version_id: str, svc=Depends(service)):

    agent = _agent(svc)
    row = _owned_version(svc.db, agent, version_id)
    return _call(lambda: _version(row, store.read(agent.id, row.path), text=True))


@router.post("/memory/versions/{version_id}/revert")
def memory_revert(version_id: str, svc=Depends(service)):

    agent = locked_agent(svc.db, _agent(svc).id)
    row = _owned_version(svc.db, agent, version_id)
    if _busy(agent): raise store.MemoryConflict(DREAMING)

    def run():
        try:
            undo = store.revert(svc.db, agent, version_id)
        except PermissionError as exc:
            raise BadRequestError("这次修改不能撤销：" + str(exc)) from None
        svc.db.commit()
        return {"reverted": version_id, "version": versions.as_dict(undo) if undo is not None else None,
                "file": _workspace_path(row.path)}

    return _call(run)


@router.get("/memory/snapshots")
def memory_snapshots(svc=Depends(service)):

    agent = _agent(svc)

    def run():
        items = [{"id": item.get("id"), "at": item.get("at"), "note": item.get("note") or "",
                  "files": len(item.get("files") or [])} for item in snapshots.listing(agent.id)]
        return {"available": True, "items": items}

    return _call(run)


@router.post("/memory/snapshots/{snapshot_id}/restore")
def memory_restore(snapshot_id: str, svc=Depends(service)):

    agent = locked_agent(svc.db, _agent(svc).id)
    if _busy(agent): raise store.MemoryConflict(DREAMING)

    def run():
        before = store.wiki(agent.id)
        with files.workspace_lock(agent.id):
            try:
                snapshots.restore(svc.db, agent, snapshot_id)
            except LookupError:
                raise ResourceNotFoundError("memory snapshot", snapshot_id) from None
            except ValueError as exc:
                raise BadRequestError(str(exc)) from None
            after = store.wiki(agent.id)
        svc.db.commit()
        changed = sorted(rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel))
        return {"restored": snapshot_id, "changed": changed}

    return _call(run)


@router.get("/memory/lint")
def memory_lint(svc=Depends(service)):

    agent = _agent(svc)

    def run():
        found = lint.check_wiki(store.wiki(agent.id), seed.schema_text(), sources.checker(svc.db, agent))
        return {"available": True, "findings": [f.as_dict() for f in found],
                "blocking": len(lint.blocking(found)), "warnings": len(found) - len(lint.blocking(found))}

    return _call(run)
