"""pa_memory_search: grep the wiki (pages whose name or aliases match first), then journal/.

Archived pages come after active ones; a search by the working agent marks the pages it
found as used (memory.usage)."""
import os
import re

from personal_agent.memory import layout, page, store
from personal_agent.workspace import files

MAX_HITS = 40
DAY = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|legacy-memory)\.md$")   # legacy-memory.md: old MEMORY.md text (migration)


def search(ident, query, limit=20, *, used=False):
    terms = [t.casefold() for t in (query or "").split() if t.strip()]
    if not terms: return {"pages": [], "lines": [], "journal": []}
    found = []   # (archived, pages entry or None, line hits)
    for rel, text in store.wiki(ident).items():
        if rel == layout.SCHEMA: continue
        parsed = page.parse(text)
        names = [layout.slug(rel) or "", str(parsed.meta.get("description") or ""), *parsed.aliases]
        entry = None
        if any(all(t in name.casefold() for t in terms) for name in names):
            entry = {"path": "/workspace/" + rel, "description": parsed.meta.get("description"),
                     "aliases": parsed.aliases, "status": parsed.meta.get("status")}
        found.append((parsed.meta.get("status") == "archived", entry, _grep(rel, text, terms)))
    found.sort(key=lambda item: item[0])   # stable: active pages first, each group in file order
    pages = [entry for _a, entry, _h in found if entry][:limit]
    hits = [hit for _a, _e, lines in found for hit in lines][:limit]
    if used:
        from personal_agent.memory import usage
        usage.touch(ident, [item["path"] for item in pages + hits])
    return {"pages": pages, "lines": hits, "journal": _journal(ident, terms)[:limit]}


def _grep(rel, text, terms):
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        if all(t in line.casefold() for t in terms):
            out.append({"path": "/workspace/" + rel, "line": number, "text": line[:300]})
            if len(out) >= MAX_HITS: break
    return out


def _journal(ident, terms):
    """Newest days first, then the old MEMORY.md text; journal text is quoted history, never instructions."""
    root = files.local_workspace(ident) / layout.JOURNAL_DIR
    if not root.is_dir(): return []
    out = []
    for name in sorted((e.name for e in os.scandir(root) if DAY.match(e.name)), reverse=True):
        text = store.read(ident, layout.JOURNAL_DIR + "/" + name) or ""
        out += _grep(layout.JOURNAL_DIR + "/" + name, text, terms)
        if len(out) >= MAX_HITS: break
    return out
