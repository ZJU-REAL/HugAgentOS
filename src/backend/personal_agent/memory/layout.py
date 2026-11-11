"""Where the memory wiki lives and what may be written where.

The wiki is /workspace/memory/: a fixed _schema.md, an _index.md that the organizer
rebuilds, and flat topic pages named <type>-<name>.md. USER.md and the pinned section of
MEMORY.md stay outside it and are written only on the owner's explicit request.
"""
import re

DIR = "memory"
SCHEMA = DIR + "/_schema.md"
INDEX = DIR + "/_index.md"
USER = "USER.md"
MEMORY = "MEMORY.md"
REPORT = "DREAMS.md"
JOURNAL_DIR = "journal"       # platform-written daily records, journal/YYYY-MM-DD.md

TYPES = ("person", "org", "project", "topic", "procedure")
TYPE_LABELS = {"person": "人", "org": "组织", "project": "项目", "topic": "主题与决定", "procedure": "做法"}
STATUSES = ("active", "archived")

# <type>-<name>.md: lowercase letters, digits and single hyphens; Chinese names use pinyin.
PAGE_NAME = re.compile(r"^(" + "|".join(TYPES) + r")-[a-z0-9]+(?:-[a-z0-9]+)*$")

PAGE_MAX_BYTES = 32 * 1024
CURRENT_PICTURE_AFTER_CHARS = 8000
WIKI_MAX_BYTES = 2 * 1024 * 1024
WIKI_MAX_RATIO = 0.5

PINNED_HEADING = "## 置顶"
WHERE_TO_START = "## Where to start"
PENDING_HEADING = "## 待整理"
PINNED_MAX_CHARS = 6000
INDEX_HEAD_MAX_CHARS = 6000
WHERE_TO_START_MAX_LINES = 40

# Writers, recorded on every version: who may write what is decided in store.allowed.
ONLINE = "online"        # the agent's own turn, a channel session or a background task
ANALYZER = "analyzer"    # Dreaming, one source at a time
ORGANIZER = "organizer"  # Dreaming, the whole wiki once
USER_EDIT = "user"       # the owner in the workspace editor
SYSTEM = "system"        # seeding, migration, revert, snapshot restore
WRITERS = (ONLINE, ANALYZER, ORGANIZER, USER_EDIT, SYSTEM)


def slug(rel):
    """The page slug of memory/<slug>.md, or None for any other path."""
    parts = rel.split("/")
    if len(parts) != 2 or parts[0] != DIR or not parts[1].endswith(".md"): return None
    name = parts[1][:-3]
    return name if PAGE_NAME.match(name) else None


def is_page(rel):
    return slug(rel) is not None


def is_wiki(rel):
    """A topic page or one of the wiki's own files."""
    return is_page(rel) or rel in (SCHEMA, INDEX)


def is_memory(rel):
    """A file the memory store owns: USER.md, MEMORY.md and everything under memory/."""
    return rel in (USER, MEMORY) or rel.split("/")[0] == DIR
