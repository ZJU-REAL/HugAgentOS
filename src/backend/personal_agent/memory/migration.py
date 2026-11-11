"""One-off move from the old memory layout to the topic wiki.

The old layout kept platform-written daily journals at memory/YYYY-MM-DD.md and a free-form
MEMORY.md, where the old Dreaming added managed entries (exact owner quotes between
<!-- dream-key:… --> markers) next to whatever the agent wrote. migrate():

- moves every memory/YYYY-MM-DD.md to journal/YYYY-MM-DD.md, merging into a day that
  already exists there without duplicating a <!-- pa-journal:… --> entry;
- rewrites MEMORY.md as the new shape: the managed quotes become lines under ## 置顶, and
  every other bit of text moves verbatim to journal/legacy-memory.md (a record, never
  injected);
- records each change in the write log as a SYSTEM version, and asks the next Dreaming run
  to replay history (state.request_genesis) when there was anything to replay.

seed() calls it on every seeding, so it must be cheap and quiet. The "done" marker is
config[MARK] on the PersonalAgent row, set under the row lock in the same transaction as the
version rows. A file next to the workspace would not do: the task user owns /workspace, so
the agent (or a restored backup) can create or delete any file there, and nothing it writes
can reach the agent's config. The row lock also serializes two seedings of one agent.

File I/O walks directories with O_NOFOLLOW (workspace.files._open_dir) and opens files with
O_NOFOLLOW: a link planted in memory/ is left where it is and never read or moved.
"""
import logging
import os
import re
import stat
from datetime import date, datetime, timedelta

from personal_agent.memory import layout, state, store, versions

MARK = "memory_layout"
VERSION = 2                      # 1: daily journals in memory/; 2: wiki + journal/
LEGACY = layout.JOURNAL_DIR + "/legacy-memory.md"
REPLAY_DAYS = 90
READ_MAX = 16 * 1024 * 1024      # the old journal writer refused to grow a day past this

DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
ENTRY = re.compile(r"<!-- pa-journal:[0-9a-f]{64} -->")
MANAGED = re.compile(r"<!-- dream-key:([a-z0-9_-]{1,60}) [^\n]* -->\n(.*?)\n<!-- /dream-key:\1 -->", re.S)

# Lines of the old MEMORY.md template: platform text, not something the owner or agent wrote.
OLD_TEMPLATE = {"# MEMORY.md - 长期记忆", "精选的长期事实、决定和经验教训。详细日志写在 `memory/YYYY-MM-DD.md`。", "（暂无内容）"}
# The new shape is the MEMORY.md template seeded today (workspace/prompts/template_memory),
# with the pinned lines placed under its ## 置顶 heading. This fallback is used only when the
# template file cannot be read.
FALLBACK_TEMPLATE = "# MEMORY.md - 置顶记忆\n\n" + layout.PINNED_HEADING + "\n"

log = logging.getLogger(__name__)


def migrate(ident, sessions=None):
    """Move this workspace to the wiki layout once. True when files changed."""
    from personal_agent.workspace import files
    try:
        if not files.local_workspace(ident).is_dir(): return False
    except files.WorkspaceUnavailable:
        return False
    from core.db.engine import SessionLocal
    from personal_agent.models import PersonalAgent
    from sqlalchemy.exc import SQLAlchemyError
    try:
        with (sessions or SessionLocal)() as db:
            agent = db.get(PersonalAgent, ident)
            if agent is None or (agent.config or {}).get(MARK) == VERSION: return False
            agent = db.query(PersonalAgent).filter(PersonalAgent.id == ident).populate_existing().with_for_update().one()
            if (agent.config or {}).get(MARK) == VERSION: return False
            error = None
            with files.workspace_lock(ident):
                try:
                    changes, quotes = _move(ident)
                except (OSError, ValueError) as exc:
                    # A planted link or an oversized day: retrying on every seeding cannot fix
                    # it. Mark the layout migrated with the error; the rest stays where it is.
                    changes, quotes, error = [], [], type(exc).__name__
                    log.warning("Personal memory migration skipped (%s)", error)
                changes += _strip_user_markers(ident) + _update_agents(ident)
                days = _journal_days(ident)
                legacy = bool(days or quotes) or _exists(ident, LEGACY)
            # Journal moves are history, not memory: they are logged, not versioned.
            for path, before, after in changes:
                if path in (layout.USER, layout.MEMORY): versions.record(db, ident, path, before, after, layout.SYSTEM)
            db.flush()   # production sessions do not autoflush: baseline must see the rows above
            baseline(db, ident)
            if legacy: state.request_genesis(agent, _since(agent, days))
            agent.config = {**(agent.config or {}), MARK: VERSION, **({MARK + "_error": error} if error else {})}
            db.commit()
            if changes: log.info("Personal memory migrated (%d files)", len(changes))
            return bool(changes)
    except BlockingIOError:
        return False             # someone holds the workspace lock: the next seeding retries
    except SQLAlchemyError as exc:
        log.warning("Personal memory migration deferred (%s)", type(exc).__name__)
        return False


def baseline(db, ident):
    """A first version of every injected memory file that has none, so injection can tell a
    later write outside the store (inject.trusted)."""
    for rel in (layout.USER, layout.MEMORY, layout.INDEX):
        text = store.read(ident, rel)
        if text is not None and versions.latest(db, ident, rel) is None:
            versions.record(db, ident, rel, None, text, layout.SYSTEM)


AGENTS = "AGENTS.md"
AGENTS_NOTICE = ("\n\n## 记忆（平台已更新）\n\n记忆现在是 `memory/` 里按主题组织的 wiki，按平台注入的 Memory 规则写。"
                 "不要再写 `memory/YYYY-MM-DD.md`：平台自动记的流水账在 `journal/`，只读。\n")


def _update_agents(ident):
    """An AGENTS.md seeded from the old template still teaches the old layout: say it changed."""
    from personal_agent.workspace import files
    text = store.read(ident, AGENTS)
    if text is None or "memory/YYYY-MM-DD.md" not in text or AGENTS_NOTICE.strip() in text: return []
    after = text.rstrip("\n") + AGENTS_NOTICE
    files._write_file(ident, AGENTS, after, None)
    return [(AGENTS, text, after)]


def _strip_user_markers(ident):
    """The old Dreaming also kept managed quotes in USER.md: keep each quote, drop its markers."""
    from personal_agent.workspace import files
    text = store.read(ident, layout.USER)
    if text is None: return []
    after = MANAGED.sub(lambda m: m.group(2), text.replace("\r\n", "\n"))
    if after == text: return []
    files._write_file(ident, layout.USER, after, None)
    return [(layout.USER, text, after)]


def _move(ident):
    """All file changes, as [(path, before, after)], and the owner quotes put under ## 置顶."""
    from personal_agent.workspace import files
    root = files.local_workspace(ident)
    changes = []
    try:
        old = files._open_dir(root, [layout.DIR])
    except (FileNotFoundError, NotADirectoryError):
        old = None
    except OSError:
        old = None               # memory/ itself is a link: nothing of ours is in it
    if old is not None:
        try:
            names = sorted(e.name for e in os.scandir(old) if DAY.match(e.name) and e.is_file(follow_symlinks=False) and _real_day(e.name))
            if names:
                new = files._open_dir(root, [layout.JOURNAL_DIR], create=True)
                try:
                    for name in names: changes += _move_day(old, new, name)
                finally:
                    os.close(new)
        finally:
            os.close(old)
    memory_changes, quotes = _split_memory(ident)
    return changes + memory_changes, quotes


def _move_day(old, new, name):
    text = _read(old, name)
    if text is None: return []   # replaced by a link or a directory while we looked
    before = _read(new, name)
    after = text if before is None else merge(before, text, name[:-3])
    if after != before: _write(new, name, after)
    os.unlink(name, dir_fd=old)
    os.fsync(old)
    rel_old, rel_new = layout.DIR + "/" + name, layout.JOURNAL_DIR + "/" + name
    return [(rel_new, before, after), (rel_old, text, None)]


def merge(target, source, day):
    """source's entries appended to target, skipping any pa-journal entry target already has."""
    head, entries = _entries(source)
    out = target.rstrip("\n")
    head = head.strip("\n")
    if head and head != "# " + day and head not in target: out += "\n\n" + head
    for marker, block in entries:
        if marker not in target: out += "\n\n" + block.strip("\n")
    return out + "\n"


def _entries(text):
    found = list(ENTRY.finditer(text))
    if not found: return text, []
    ends = [m.start() for m in found[1:]] + [len(text)]
    return text[:found[0].start()], [(m.group(0), text[m.start():end]) for m, end in zip(found, ends)]


def _split_memory(ident):
    """MEMORY.md as the new shape; its old text moves to journal/legacy-memory.md."""
    from personal_agent.workspace import files
    root = files.local_workspace(ident)
    fd = files._open_dir(root, [])
    try:
        text = _read(fd, layout.MEMORY)
    finally:
        os.close(fd)
    if text is None: return [], []
    original, text = text, text.replace("\r\n", "\n")
    quotes = [m.group(2) for m in MANAGED.finditer(text)]
    rest = MANAGED.sub("", text)
    pinned_lines = [line for line in _section(rest, layout.PINNED_HEADING).splitlines()[1:] if line.strip()]
    rest = _drop_section(rest, layout.PINNED_HEADING)
    pinned = pinned_lines + [_line(q) for q in quotes if _line(q) not in pinned_lines]
    template_text = _template_text()
    after = _with_pinned(template_text, pinned)
    changes = []
    template = OLD_TEMPLATE | {line.strip() for line in template_text.splitlines() if line.strip()}
    old_text = any(line.strip() and line.strip() not in template for line in rest.splitlines())
    old_template = any(line.strip() in OLD_TEMPLATE - {"（暂无内容）"} for line in text.splitlines())
    if not quotes and not old_text and not old_template: return [], []   # already the new shape
    if old_text:
        new = files._open_dir(root, [layout.JOURNAL_DIR], create=True)
        try:
            before = _read(new, "legacy-memory.md")
            kept = "\n".join(line.rstrip() for line in rest.strip("\n").splitlines())
            if before is None or kept not in before:
                body = ("# MEMORY.md 旧内容\n\n迁移到记忆 wiki 时从 MEMORY.md 移出的原文，只作历史记录，不注入对话。\n\n" if before is None else before.rstrip("\n") + "\n\n") + kept + "\n"
                _write(new, "legacy-memory.md", body)
                changes.append((LEGACY, before, body))
        finally:
            os.close(new)
    if after != original:
        files._write_file(ident, layout.MEMORY, after, None)
        changes.append((layout.MEMORY, original, after))
    return changes, quotes


def _template_text():
    """The MEMORY.md template seeded today; its lines never count as legacy text."""
    from personal_agent.workspace.constants import PROMPT_DIR, TEMPLATE_PARTS
    try: text = (PROMPT_DIR / (TEMPLATE_PARTS[layout.MEMORY] + ".system.md")).read_text(encoding="utf-8")
    except OSError: return FALLBACK_TEMPLATE
    return text if layout.PINNED_HEADING in text.splitlines() else FALLBACK_TEMPLATE


def _with_pinned(template_text, pinned):
    """The template with the pinned lines right under its ## 置顶 heading."""
    out = []
    for line in template_text.rstrip("\n").splitlines():
        out.append(line)
        if line.strip() == layout.PINNED_HEADING: out += pinned
    return "\n".join(out) + "\n"


def _line(quote):
    """One owner quote as a bullet; a quote of several lines keeps them, indented."""
    return "- " + "\n  ".join(quote.strip("\n").splitlines())


def _section(text, heading):
    from personal_agent.memory import page
    return page.section(text, heading)


def _drop_section(text, heading):
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "): inside = line.strip() == heading
        if not inside: out.append(line)
    return "\n".join(out)


def _journal_days(ident):
    from personal_agent.workspace import files
    try:
        fd = files._open_dir(files.local_workspace(ident), [layout.JOURNAL_DIR])
    except OSError:
        return []
    try:
        return sorted(m.group(1) for e in os.scandir(fd) if (m := DAY.match(e.name)) and _real_day(e.name))
    finally:
        os.close(fd)


def _exists(ident, rel):
    from personal_agent.workspace import files
    return files._raw(ident, rel) is not None


def _since(agent, days):
    """Replay from the oldest legacy day, but never more than REPLAY_DAYS back."""
    from personal_agent.scheduling.schedules import user_zone, zone
    tz = zone(user_zone(agent.config))
    floor = datetime.now(tz) - timedelta(days=REPLAY_DAYS)
    if not days: return floor.isoformat()
    oldest = datetime.combine(date.fromisoformat(days[0]), datetime.min.time(), tz)
    return max(oldest, floor).isoformat()


def _real_day(name):
    try: date.fromisoformat(name[:10])
    except ValueError: return False
    return True


def _read(dir_fd, name):
    """A regular file's text, or None when it is missing, a link or anything else."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except (FileNotFoundError, OSError):
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode): return None
        if info.st_size > READ_MAX: raise ValueError("Journal too large to migrate")
        return handle.read(READ_MAX + 1).decode("utf-8", errors="replace")


def _write(dir_fd, name, text):
    """Atomic replace inside an already opened directory (journal/ is not a memory file)."""
    from personal_agent.workspace import files
    temporary = "." + name + "." + os.urandom(6).hex() + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        files._own(temporary, dir_fd)
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        try: os.unlink(temporary, dir_fd=dir_fd)
        except OSError: pass
        raise
