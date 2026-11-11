"""Archive what nobody uses, delete what stays unused; done by code in each Dreaming run.

A page is alive while the agent uses it (memory.usage) or a new source cites it. Idle for
ARCHIVE_IDLE_DAYS, it is archived: it stays listed under the index's archive group, out of
Where to start, and ranks after active pages in search. Archived and still unused for
DELETE_IDLE_DAYS, it is deleted; the run's snapshot and the deletion's version keep its text,
so the owner can bring it back from the memory panel. Using or citing an archived page makes
it active again. The run's rollback undoes all of it with the rest of the run.
"""
from datetime import date, datetime, timedelta, timezone

from personal_agent.memory import layout, page, store, usage
from personal_agent.memory.models import PersonalMemoryUsage

ARCHIVE_IDLE_DAYS = 60
DELETE_IDLE_DAYS = 90
STATUS_LINE = "status:"


def _day(moment):
    return moment.astimezone(timezone.utc).date() if moment else None


def _last_alive(text, row):
    """The newest of: last use, first seen, the newest as-of of a source tag."""
    days = [_day(row.first_seen_at), _day(row.last_used_at)]
    for match in page.SOURCE_TAG.finditer(text):
        try: days.append(date.fromisoformat(match[2]))
        except ValueError: pass
    return max(d for d in days if d)


def set_status(text, status):
    """The page with its frontmatter status replaced; unchanged when it has no status line."""
    head, sep, body = text.partition("\n---\n")
    if not text.startswith("---\n") or not sep: return text
    lines = [STATUS_LINE + " " + status if line.startswith(STATUS_LINE) else line for line in head.splitlines()]
    return "\n".join(lines) + sep + body


def apply(db, agent, today, *, run_id=None, now=None):
    """{"archived", "restored", "deleted"}: slugs this pass changed. today: the owner's date."""
    now = now or datetime.now(timezone.utc)
    today = date.fromisoformat(today) if isinstance(today, str) else today
    known = usage.rows(db, agent.id)
    out = {"archived": [], "restored": [], "deleted": []}
    for rel, text in store.wiki(agent.id).items():
        if not layout.is_page(rel) or page.stub_target(text) is not None: continue
        row = known.get(rel)
        if row is None:   # seen, not used yet: its clock starts now
            row = PersonalMemoryUsage(agent_id=agent.id, path=rel, first_seen_at=now, uses=0)
            db.add(row)
        status = page.parse(text).meta.get("status")
        alive = _last_alive(text, row)
        if status == "archived":
            if row.archived_at is None:
                row.archived_at = now          # archived by hand or by an older run: start its clock
            elif alive > _day(row.archived_at):
                _write(db, agent, rel, set_status(text, "active"), run_id)
                row.archived_at = None
                out["restored"].append(layout.slug(rel))
            elif (today - _day(row.archived_at)).days >= DELETE_IDLE_DAYS:
                store.remove(db, agent, rel, text, writer=layout.ORGANIZER, run_id=run_id)
                db.delete(row)
                out["deleted"].append(layout.slug(rel))
        elif (today - alive).days >= ARCHIVE_IDLE_DAYS:
            changed = set_status(text, "archived")
            if changed != text:
                _write(db, agent, rel, changed, run_id)
                row.archived_at = now
                out["archived"].append(layout.slug(rel))
    db.flush()
    return out


def _write(db, agent, rel, content, run_id):
    # A status flip is not a content change: lint judged the page when it was written.
    store.write(db, agent, rel, content, None, writer=layout.ORGANIZER, run_id=run_id, check=False)
