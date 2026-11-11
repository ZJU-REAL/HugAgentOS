"""What of memory goes into every turn's system prompt: small and trusted only.

USER.md whole (as before), the pinned section of MEMORY.md, and the head of _index.md
(Where to start plus the group headings). Pages are read on demand.
"""
from personal_agent.memory import layout, page, versions


def pinned(memory_text):
    """The ## 置顶 section of MEMORY.md, heading included; "" when it is absent or empty."""
    text = page.section(memory_text or "", layout.PINNED_HEADING)[: layout.PINNED_MAX_CHARS].strip()
    return text if text.strip() != layout.PINNED_HEADING else ""


def trusted(db, agent_id, rel, text):
    """The file is what the memory store last wrote. The sandbox can write /workspace directly
    (its Bash runs as the workspace owner); such a write skipped lint and the lease, so it is
    not injected until the owner saves the file again. A file with no version is trusted:
    migration records a baseline for every injected file (migration.baseline)."""
    row = versions.latest(db, agent_id, rel)
    return row is None or row.after_hash == versions.digest(text)


def index_head(index_text):
    """Where to start, then each group heading with its page count; never the page lines."""
    text = index_text or ""
    start = page.section(text, layout.WHERE_TO_START)
    groups, current, count = [], None, 0
    for line in text.splitlines():
        if line.startswith("## "):
            if current and current != layout.WHERE_TO_START: groups.append(current + "（" + str(count) + " 页）")
            current, count = line.strip(), 0
        elif line.startswith("- [[") and current:
            count += 1
    if current and current != layout.WHERE_TO_START: groups.append(current + "（" + str(count) + " 页）")
    body = "\n".join(part for part in (start, "\n".join(groups)) if part)
    return body[: layout.INDEX_HEAD_MAX_CHARS].strip()
