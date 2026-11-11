"""The one write path for memory files: USER.md, MEMORY.md and everything under memory/.

Every writer goes through write(), edit() or remove(): the agent's turn and background
tasks (ONLINE), the editor (USER_EDIT), Dreaming (ANALYZER, ORGANIZER) and seeding,
migration and revert (SYSTEM). Each call takes the workspace lock, checks who may write
the path, lints a page, writes atomically and adds a version to the caller's transaction.
The caller commits; a file written whose version then fails to commit stays written.
"""
import os
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import select

from core.infra.exceptions import AppException, BadRequestError, ResourceNotFoundError
from personal_agent.memory import layout, lint, page, state, versions
from personal_agent.memory.models import PersonalMemoryVersion
from personal_agent.memory.sources import SOURCE_ID, checker, parse
from personal_agent.models import PersonalAgent
from personal_agent.workspace import files

LOCKED = "记忆文件正在写入（可能是 Dreaming 或另一次保存），请稍后再试"
BUSY = "记忆正在整理（Dreaming 运行中），请过几分钟再写；这次对话会在下次整理时补进记忆"


class MemoryConflict(AppException):
    """409: the file changed since it was read, or Dreaming holds the memory lease."""

    def __init__(self, message):
        super().__init__(code=41002, message=message, status_code=409)


class MemoryLintError(BadRequestError):
    """A page failed a blocking lint check; findings say what to fix."""

    def __init__(self, findings):
        self.findings = findings
        super().__init__("页面没有保存：" + "；".join(f.detail for f in findings), data={"findings": [f.as_dict() for f in findings]})


def allowed(writer, rel):
    if rel == layout.SCHEMA: return writer == layout.SYSTEM
    if writer == layout.ONLINE: return layout.is_page(rel) or rel in (layout.USER, layout.MEMORY)
    if writer in (layout.ANALYZER, layout.ORGANIZER): return layout.is_page(rel) or rel == layout.INDEX
    if writer in (layout.USER_EDIT, layout.SYSTEM): return layout.is_wiki(rel) or rel in (layout.USER, layout.MEMORY)
    return False


is_memory = layout.is_memory   # other workspace files keep using workspace.files


def read(ident, rel):
    data = files._raw(ident, files.relative(rel))
    return None if data is None else data.decode("utf-8", errors="replace")


def wiki(ident):
    """{rel: text} of every .md file directly under memory/."""
    root = files.local_workspace(ident) / layout.DIR
    if not root.is_dir(): return {}
    out = {}
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not entry.name.endswith(".md") or entry.name.startswith(".") or not entry.is_file(follow_symlinks=False): continue
        rel = layout.DIR + "/" + entry.name
        text = read(ident, rel)
        if text is not None: out[rel] = text
    return out


@contextmanager
def _locked(ident):
    """The workspace lock; a writer still holding it after the wait is a 409, not a 500."""
    try:
        with files.workspace_lock(ident):
            yield
    except BlockingIOError:
        raise MemoryConflict(LOCKED) from None


def write(db, agent, path, content, expected, *, writer, source_id=None, run_id=None, check=True):
    """Create or replace a memory file. expected: the etag the writer read, "" for a new file,
    None to skip the check (Dreaming and SYSTEM writes hold the lock for the whole run)."""
    rel = files.relative(path)
    _check(agent, rel, writer, source_id)
    with _locked(agent.id):
        _check_lease(db, agent, rel, writer)
        before = read(agent.id, rel)
        if check: _lint(db, agent, rel, before, content, writer, source_id)
        try:
            saved = files._write_file(agent.id, rel, content, expected)
        except ValueError as exc:
            if "changed" in str(exc): raise MemoryConflict("文件在你读取之后被改过，请重新读取后再改") from exc
            raise BadRequestError(str(exc)) from exc
        version = versions.record(db, agent.id, rel, before, content, writer, source_id=source_id, run_id=run_id)
        if before is None and layout.is_page(rel) and writer in (layout.ONLINE, layout.USER_EDIT):
            _list_pending(db, agent, rel, content, source_id)
    return {**saved, "version": version}


def edit(db, agent, path, old, new, *, writer, source_id=None, run_id=None):
    """Replace one exact, unique passage."""
    rel = files.relative(path)
    current = files.read_file(agent.id, rel)
    text = current["content"] or ""
    if not old or text.count(old) != 1: raise BadRequestError("old_string 必须在文件中恰好出现一次，请重新读取")
    return write(db, agent, rel, text.replace(old, new, 1), current["etag"], writer=writer, source_id=source_id, run_id=run_id)


def remove(db, agent, path, expected_text, *, writer, run_id=None):
    """Delete a page only while it still holds expected_text (CAS); returns the version or None.

    A SYSTEM removal (revert of a creation) also drops the page's lines from _index.md, so the
    index never lists a page that is gone."""
    rel = files.relative(path)
    if writer not in (layout.SYSTEM, layout.ORGANIZER) or not layout.is_page(rel):
        raise PermissionError("只有 Dreaming 收尾和撤销可以删除页面")
    with _locked(agent.id):
        current = read(agent.id, rel)
        if current is None: return None
        if current != expected_text: raise MemoryConflict("页面已被改动，没有删除：" + rel)
        fd = files._open_dir(files.local_workspace(agent.id), [layout.DIR])
        try:
            os.unlink(rel.split("/", 1)[1], dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        version = versions.record(db, agent.id, rel, current, None, writer, run_id=run_id)
        if writer == layout.SYSTEM: _unlist(db, agent, layout.slug(rel), run_id)
        return version


def revert(db, agent, version_id):
    """Undo one change by writing a new version; the history is never edited.

    Only while the file still holds exactly what that change wrote (checked under the lock
    by etag): a later change on top of it would be lost, so the owner edits by hand instead."""
    row = db.get(PersonalMemoryVersion, version_id)
    if row is None or row.agent_id != agent.id: raise ResourceNotFoundError("memory version", version_id)
    if row.reverted_by: raise BadRequestError("这次修改已经撤销过了")
    if not revertable(row, read(agent.id, row.path)):
        raise BadRequestError("这个文件在这次修改之后又被改过，请在编辑器里手动修改")
    if row.before_text is None:
        undo = remove(db, agent, row.path, row.after_text, writer=layout.SYSTEM)
    else:
        expected = "" if row.after_text is None else files.etag(row.after_text.encode("utf-8"))
        undo = write(db, agent, row.path, row.before_text, expected, writer=layout.SYSTEM, check=False)["version"]
        # A deleted page brought back (an archived page Dreaming removed): list it again.
        if row.after_text is None and layout.is_page(row.path):
            with _locked(agent.id): _list_pending(db, agent, row.path, row.before_text, None)
    row.reverted_by = undo.id if undo is not None else "noop"
    return undo


def revertable(row, current):
    """Revert can work now: not yet undone, the file unchanged since, and a page if it was created."""
    if row.reverted_by or versions.digest(current) != row.after_hash: return False
    return row.before_text is not None or layout.is_page(row.path)


def _check(agent, rel, writer, source_id):
    if writer not in layout.WRITERS: raise ValueError("Unknown memory writer")
    if not allowed(writer, rel):
        raise PermissionError("不能写 " + rel + "：" + _why(writer, rel))
    if _leased(rel, writer) and state.busy(agent): raise MemoryConflict(BUSY)
    if writer in (layout.ONLINE, layout.ANALYZER) and layout.is_page(rel) and not SOURCE_ID.match(source_id or ""):
        raise ValueError("A page write needs the source id of the current turn or task")
    # USER.md and the pinned section hold the owner's explicit words and are injected into
    # every turn: a background task or an event-only turn does not write them.
    if writer == layout.ONLINE and rel in (layout.USER, layout.MEMORY) and (parse(source_id) or ("",))[0] not in ("conv", "chan"):
        raise PermissionError("USER.md 和 MEMORY.md 只在用户本人的对话里、按用户的原话写")


def _leased(rel, writer):
    """The lease guards memory/, the only part Dreaming writes and snapshots. USER.md and
    MEMORY.md stay writable during a run: an owner's "记住：…" must not wait for it."""
    return writer in (layout.ONLINE, layout.USER_EDIT) and rel.split("/")[0] == layout.DIR


def _check_lease(db, agent, rel, writer):
    """Again under the lock, from the committed row: Dreaming may have claimed since agent was loaded."""
    if not _leased(rel, writer): return
    config = db.scalar(select(PersonalAgent.config).where(PersonalAgent.id == agent.id))
    if state.busy(SimpleNamespace(config=config)): raise MemoryConflict(BUSY)


def _lint(db, agent, rel, before, content, writer, source_id):
    if layout.is_page(rel):
        # A citation the page already had is not re-judged: a conversation the owner deleted
        # later must not freeze every page that cites it.
        exists, old = checker(db, agent), set(page.sources(before or ""))
        known = (lambda sid: sid in old or exists(sid)) if writer in (layout.ONLINE, layout.ANALYZER) else None
        found = lint.blocking(lint.check_page(rel, content, known, allow_stub=writer == layout.ORGANIZER))
        if writer in (layout.ONLINE, layout.ANALYZER, layout.ORGANIZER):
            found += lint.check_new_duplicates(rel, before, content)
        if writer == layout.ONLINE:
            found += lint.check_new_facts(rel, before, content, source_id)
        elif writer in (layout.ANALYZER, layout.ORGANIZER):
            cited = {sid for text in wiki(agent.id).values() for sid in page.sources(text)}
            found += lint.check_new_citations(rel, content, cited | ({source_id} if source_id else set()))
    elif rel in (layout.USER, layout.MEMORY) and writer == layout.ONLINE:
        found = lint.blocking(lint.check_note(rel, content))
    else:
        return
    if found: raise MemoryLintError(found)


def _why(writer, rel):
    if rel == layout.SCHEMA: return "_schema.md 是平台规则，任何人都不能改"
    if rel == layout.INDEX: return "_index.md 由 Dreaming 维护；新页面会自动列进“待整理”"
    if layout.DIR == rel.split("/")[0]: return "页面文件名必须是 memory/<type>-<name>.md"
    return "这个文件不属于记忆"


def _unlist(db, agent, name, run_id):
    before = read(agent.id, layout.INDEX)
    if not before: return
    after = "\n".join(line for line in before.splitlines() if not line.startswith("- [[" + name + "]]")) + "\n"
    if after == before: return
    files._write_file(agent.id, layout.INDEX, after, None)
    versions.record(db, agent.id, layout.INDEX, before, after, layout.SYSTEM, run_id=run_id)


def _list_pending(db, agent, rel, content, source_id):
    """A page made during the day is listed under ## 待整理 until the organizer places it."""
    name = layout.slug(rel)
    description = (page.parse(content).meta.get("description") or "").replace("\n", " ")[:120]
    before = read(agent.id, layout.INDEX) or ""
    if "[[" + name + "]]" in before: return
    text = before.rstrip("\n")
    if layout.PENDING_HEADING not in text.splitlines():
        text += ("\n\n" if text else "") + layout.PENDING_HEADING
    after = text + "\n- [[" + name + "]]：" + description + "\n"
    files._write_file(agent.id, layout.INDEX, after, None)
    versions.record(db, agent.id, layout.INDEX, before or None, after, layout.SYSTEM, source_id=source_id)

