"""What an agent's workspace should hold: its built-in skills plus the skills and plugins its owner
bound and is still allowed to use, each with the source tree to copy."""
import functools
import json
from dataclasses import dataclass, field

from personal_agent.capability_sync import tree as trees


class CatalogUnavailable(RuntimeError):
    """The grants could not be read this time. Nothing is removed on a guess."""


@dataclass
class Entry:
    name: str
    source: str  # "builtin" | "selected" | "plugin"
    tree: dict = None
    hash: str = ""
    error: str = ""  # set when the source could not be read
    extra: dict = field(default_factory=dict)  # what a plugin's manifest entry records besides its hash


@dataclass
class Plan:
    skills: dict  # {skill_id: Entry}
    plugins: dict  # {slug: Entry}


@functools.cache
def builtin_entries():
    """The built-in skills as entries, read and hashed once per process."""
    from personal_agent.skills import builtin_skills
    out = {}
    for skill in builtin_skills():
        try:
            data = trees.read_dir(skill["dir"])
            out[skill["id"]] = Entry(skill["id"], "builtin", data, trees.digest(data))
        except (OSError, ValueError) as exc:
            out[skill["id"]] = Entry(skill["id"], "builtin", error=str(exc))
    return out


def builtin_hashes():
    return {ident: entry.hash for ident, entry in builtin_entries().items() if not entry.error}


def selection(db, user_id, config):
    """The owner's binding resolved against what they may use now:
    {"skill_ids": [...], "plugins": [{"id", "name", "skill_ids", "mcp_ids"}]}. Read-only."""
    from personal_agent.identity import capabilities
    try:
        available = capabilities.available_capabilities(db, user_id)
    except ValueError as exc:
        raise CatalogUnavailable(str(exc)) from None
    resolved = capabilities.resolve_selection(capabilities.selection(config), available)
    return {"skill_ids": resolved["skill_ids"], "plugins": resolved["plugins"]}


def plugin_records(db, install_ids):
    """{install_id: {"slug", "name", "version", "description"}} from the installation records."""
    if not install_ids: return {}
    from core.db.models import InstalledPlugin
    rows = db.query(InstalledPlugin).filter(InstalledPlugin.install_id.in_(list(install_ids))).all()
    return {row.install_id: {"slug": row.slug, "name": row.name, "version": row.version or "",
                             "description": row.description or ""} for row in rows}


def build(chosen, records, sources=None):
    """The plan for one agent. chosen is selection(); records is plugin_records(). Skill dirs come
    from the core skill loader (a DB skill is materialized to its cache first). sources caches
    {skill_id: (tree, error)} across the agents of one sweep, so a skill is read once."""
    sources = {} if sources is None else sources

    def source(ident):
        if ident not in sources:
            try:
                from core.capabilities.skills.loader_factory import get_skill_loader
                path = get_skill_loader().get_skill_dir(ident)
                sources[ident] = (trees.read_dir(path), "") if path else (None, "unavailable")
            except (OSError, ValueError) as exc:
                sources[ident] = (None, reason(exc))
        return sources[ident]

    skills = dict(builtin_entries())
    for ident in chosen["skill_ids"]:
        if ident in skills: continue  # a built-in keeps its name
        data, error = source(ident)
        skills[ident] = Entry(ident, "selected", data, trees.digest(data) if data is not None else "", error)
    plugins = {}
    for plugin in chosen["plugins"]:
        record = records.get(plugin["id"]) or {}
        slug = record.get("slug") or plugin["id"].split("@", 1)[0]
        if slug in plugins: continue
        layout = {ident: "skills/" + ident for ident in plugin.get("skill_ids") or []}
        manifest = {"name": slug, "display_name": record.get("name") or plugin.get("name") or slug,
                    "version": record.get("version") or "", "description": record.get("description") or ""}
        files = {"plugin.json": (json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n", False)}
        error = ""
        for ident, rel in layout.items():
            data, failed = source(ident)
            if data is None:
                error = error or (ident + ": " + failed)
                continue
            files.update(trees.nest(rel, data))
        plugins[slug] = Entry(slug, "plugin", None if error else files, "" if error else trees.digest(files), error,
                              extra={"install_id": plugin["id"], "name": manifest["display_name"],
                                     "description": manifest["description"], "skills": layout,
                                     "mcp_ids": list(plugin.get("mcp_ids") or [])})
    return Plan(skills, plugins)


def reason(exc):
    """What the owner sees: an OSError's own words without the host path it carries."""
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    return str(exc) or type(exc).__name__
