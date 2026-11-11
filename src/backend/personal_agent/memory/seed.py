"""Seed the wiki: the fixed _schema.md and an empty _index.md, never any pages.

Called from persona.ensure_seeded for every workspace, new or old; it also runs the
one-off migration of the old daily-journal layout (migration.migrate is idempotent).
"""
from pathlib import Path

from personal_agent.memory import layout, store

TEMPLATES = Path(__file__).resolve().parent / "templates"


def schema_text():
    return (TEMPLATES / "_schema.md").read_text(encoding="utf-8")


def _missing(ident):
    """[(rel, template)] that must be written: a missing file, or a _schema.md that differs."""
    out = []
    for rel, name in ((layout.SCHEMA, "_schema.md"), (layout.INDEX, "_index.md")):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        current = store.read(ident, rel)
        # _schema.md is put back whenever it differs: no one may change it.
        if current is None or (rel == layout.SCHEMA and current != text): out.append((rel, text))
    return out


def seed(ident, config=None):
    """Cheap when nothing is due: it runs on every prompt build. With the agent's config, a
    finished migration is not even looked up."""
    from personal_agent.memory import migration
    from personal_agent.workspace import files
    if _missing(ident):
        with files.workspace_lock(ident):
            for rel, text in _missing(ident):
                files._write_file(ident, rel, text, None)
    if config is None or config.get(migration.MARK) != migration.VERSION:
        migration.migrate(ident)
