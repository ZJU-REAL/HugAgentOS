"""Personal Agent capability choices and current account grants."""
import logging

DEFAULT_MCP_IDS = ("internet_search", "web_fetch")
DEFAULT_SKILL_IDS = (
    "officecli-docx", "officecli-xlsx", "officecli-pptx", "pdf-editing",
)
SELECTION_KEYS = ("skill_ids", "mcp_ids", "plugin_ids")

def blocked(ident):
    """The Canvas browser is separate from the personal desktop, including legacy IDs."""
    return (ident == "browser_runtime" or ident == "browser-automation"
            or ident.endswith(":browser-automation")
            or ident.startswith(("browser-automation-", "browser-automation@")))

def default_capabilities(db, user_id):
    """The curated skills and tools this user is allowed to use, plus the plugins they have turned on,
    as {"skill_ids", "mcp_ids", "plugins"}."""
    from core.capabilities.catalog.resolver import resolve_all_runtime_enabled
    skills, _, mcps = resolve_all_runtime_enabled(db, user_id)
    # None: the catalog could not be read this time; the caller keeps what it has.
    pick = lambda curated, permitted: None if permitted is None else [ident for ident in curated if ident in permitted]
    return {"skill_ids": pick(DEFAULT_SKILL_IDS, skills), "mcp_ids": pick(DEFAULT_MCP_IDS, mcps),
            "plugins": None if skills is None or mcps is None else installed_plugins(db, user_id, set(skills), set(mcps))}

def plugin_inventory(db, user_id):
    """Read this owner's installed plugins, including disabled component identities."""
    from features.desktop.capabilities import device_catalog
    from core.capabilities.plugins import management
    try:
        # A savepoint: the agent's own rows may be pending in this transaction.
        with db.begin_nested():
            visible = management.list_installed(db, owner_user_id=user_id, include_global=True)
            private = management.list_installed(db, owner_user_id=user_id, include_global=False)
            device = device_catalog.plugin_entries(user_id=user_id)
            items = device_catalog.merge_items(visible, device, key="slug")
            # Presentation prefers one installation per slug. Hidden installations
            # still own their components, but must not become selectable plugins.
            shown = {item["install_id"] for item in items}
            items += [{**item, "enabled": False, "callable": False}
                      for item in [*visible, *private, *device] if item["install_id"] not in shown]
    except Exception:  # noqa: BLE001 - a broken plugin listing must not keep the agent from opening
        logging.getLogger(__name__).warning("personal agent plugin listing failed user=%s", user_id, exc_info=True)
        return None
    return items

def installed_plugins(db, user_id, skills, mcps, items=None):
    items = plugin_inventory(db, user_id) if items is None else items
    if items is None: return None
    out = []
    for item in items:
        if blocked(item.get("slug", "")) or blocked(item.get("install_id", "")): continue
        if not (item.get("enabled") and item.get("callable", True)): continue
        own_skills = [ident for ident in item.get("skills") or [] if ident in skills]
        own_mcps = [ident for ident in item.get("mcp") or [] if ident in mcps]
        if own_skills or own_mcps:
            out.append({"id": item["install_id"], "name": item.get("name") or item.get("slug") or item["install_id"],
                        "skill_ids": own_skills, "mcp_ids": own_mcps})
    return out

def available_capabilities(db, user_id):
    from core.capabilities.catalog.resolver import resolve_all_runtime_enabled
    skills, _, mcps = resolve_all_runtime_enabled(db, user_id)
    if skills is None or mcps is None:
        raise ValueError("Capability catalog unavailable; try again")
    items = plugin_inventory(db, user_id)
    if items is None:
        raise ValueError("Capability catalog unavailable; try again")
    denied = {ident for item in items
              if blocked(item.get("slug", "")) or blocked(item.get("install_id", ""))
              for ident in [item["install_id"], *(item.get("skills") or []), *(item.get("mcp") or [])]}
    components = {"skill_ids": sorted({i for item in items for i in item.get("skills") or []}),
                  "mcp_ids": sorted({i for item in items for i in item.get("mcp") or []})}
    plugins = installed_plugins(db, user_id, set(skills), set(mcps), items=items)
    plugins = [{**p, **{key: [i for i in p[key] if not blocked(i) and i not in denied]
                       for key in ("skill_ids", "mcp_ids")}} for p in plugins]
    return {"skill_ids": [i for i in skills if not blocked(i) and i not in denied and i not in components["skill_ids"]],
            "mcp_ids": [i for i in mcps if not blocked(i) and i not in denied and i not in components["mcp_ids"]],
            "plugins": plugins, "blocked_ids": sorted(denied), "plugin_component_ids": components}

def selection(config):
    """Initialize existing accounts once from their current bindings, not future defaults."""
    saved = config.get("capability_selection")
    if saved is not None:
        return {key: list(saved.get(key, [])) for key in SELECTION_KEYS}
    return {"skill_ids": list(config.get("skill_ids", [])),
            "mcp_ids": list(config.get("mcp_ids", [])),
            "plugin_ids": [p["id"] for p in config.get("plugins", []) if not blocked(p["id"])]}

def resolve_selection(chosen, available):
    return {**{key: [i for i in chosen[key] if i in available[key] and not blocked(i)]
               for key in ("skill_ids", "mcp_ids")},
            "plugins": [p for p in available["plugins"] if p["id"] in chosen["plugin_ids"] and not blocked(p["id"])]}

def reconcile(config, available):
    chosen = {key: [i for i in ids if not blocked(i) and i not in available.get("blocked_ids", [])
                   and i not in available.get("plugin_component_ids", {}).get(key, [])]
              for key, ids in selection(config).items()}
    config["capability_selection"] = chosen
    config.update(resolve_selection(chosen, available))

def validate_selection(patch, available, previous):
    for key in SELECTION_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if not isinstance(value, list) or len(value) > 500 or any(not isinstance(i, str) for i in value):
            raise ValueError("Capability selections must be lists of IDs")
        permitted = {p["id"] for p in available["plugins"]} if key == "plugin_ids" else set(available[key])
        # Retaining an unavailable previous choice does not grant it at runtime.
        permitted.update(previous.get(key, []))
        if any(blocked(i) or i in available.get("blocked_ids", []) or i in available.get("plugin_component_ids", {}).get(key, []) or i not in permitted for i in value):
            raise ValueError("Selected capability is unavailable or not allowed")
        patch[key] = list(dict.fromkeys(value))

def runtime_config(db, user_id, config):
    """Recheck grants before each execution; unavailable catalogs cannot grant tools."""
    try:
        available = available_capabilities(db, user_id)
    except ValueError:
        available = {"skill_ids": [], "mcp_ids": [], "plugins": []}
    return {**config, **resolve_selection(selection(config), available)}

def task_mcp_ids(config, plugins=()):
    """The MCP servers a task connects at start: the user's selection, plus those of the plugins
    this task already loaded (see personal_agent/tools/plugins.py). Other plugins' servers
    connect only when the task loads the plugin."""
    return list(dict.fromkeys(i for i in [*(config or {}).get("mcp_ids", []),
        *(i for plugin in plugins for i in plugin.mcp_ids)] if not blocked(i)))

# Skills and plugins are files in the workspace, placed there by the capability sync
# (SKILL.md per /workspace/skills/<id>/, a bundle per /workspace/plugins/<slug>/). Each
# directory keeps a manifest of what the platform synced; any other skill directory is the
# agent's own. A task only reads what is there, and lists what it may still use.
SKILLS, PLUGINS, MANIFEST = "skills", "plugins", ".synced.json"
SKILL_SUMMARY_LENGTH = 160

def synced(ident, kind):
    """The manifest entries of /workspace/<kind>/.synced.json ("skills" or "plugins"), {} without one."""
    import json
    from personal_agent.workspace.files import WorkspaceUnavailable, read_text
    try: data = json.loads(read_text(ident, kind + "/" + MANIFEST) or "{}")
    except (ValueError, WorkspaceUnavailable): return {}
    entries = data.get(kind) if isinstance(data, dict) and data.get("version") == 1 else None
    return {k: v for k, v in entries.items() if isinstance(v, dict) and _name(k)} if isinstance(entries, dict) else {}

def _name(part):
    """One path component the manifest may name: no separators, not hidden."""
    return isinstance(part, str) and part and "/" not in part and "\\" not in part and not part.startswith(".")

def skill_description(ident, rel):
    """The frontmatter description of the workspace file rel, one bounded line; None when it is missing."""
    from personal_agent.workspace.files import WorkspaceUnavailable, read_text
    try: text = read_text(ident, rel)
    except WorkspaceUnavailable: return None
    if text is None: return None
    try:
        from core.capabilities.skills.registry import split_frontmatter
        raw = split_frontmatter(text)[0].get("description") or ""
    except Exception:  # noqa: BLE001 - without a description the path still works
        raw = ""
    line = " ".join(str(raw).split())
    return line[:SKILL_SUMMARY_LENGTH - 1] + "…" if len(line) > SKILL_SUMMARY_LENGTH else line

def task_skills(ident, config):
    """The workspace skills this task may use, as [(skill_id, path, description)]: the agent's own,
    the built-in ones, and the selected catalog skills config (from runtime_config) still grants."""
    import os
    from personal_agent.workspace.constants import WORKSPACE
    from personal_agent.workspace.files import WorkspaceUnavailable, local_workspace
    try:
        with os.scandir(local_workspace(ident) / SKILLS) as entries:
            names = sorted(e.name for e in entries if _name(e.name) and e.is_dir(follow_symlinks=False))
    except (WorkspaceUnavailable, OSError):
        return []
    platform = synced(ident, SKILLS)
    granted = {i for i in (config or {}).get("skill_ids", []) if not blocked(i)}
    out = []
    for name in names:
        # A directory the manifest does not name is the agent's own, even when a platform skill has its id.
        source = platform[name].get("source") if name in platform else "own"
        if source not in ("own", "builtin", "selected") or (source == "selected" and name not in granted): continue
        rel = SKILLS + "/" + name + "/SKILL.md"
        description = skill_description(ident, rel)
        if description is not None: out.append((name, WORKSPACE + "/" + rel, description))
    return out

def task_plugins(ident, config):
    """The synced plugins config (from runtime_config) still grants, each a DeferredPlugin with only
    the granted skills and MCP servers, sorted by slug. Their skill paths: plugin_skill_paths."""
    from core.capabilities.plugins.runtime.models import DeferredPlugin
    granted = {p["id"]: p for p in (config or {}).get("plugins", []) if not blocked(p["id"])}
    out = []
    for slug, entry in sorted(synced(ident, PLUGINS).items()):
        grant = granted.get(entry.get("install_id"))
        if grant is None or blocked(slug): continue
        skills = [i for i in (entry.get("skills") or {}) if i in grant.get("skill_ids", [])]
        mcps = [i for i in entry.get("mcp_ids") or [] if i in grant.get("mcp_ids", [])]
        out.append(DeferredPlugin(install_id=grant["id"], slug=slug, name=str(entry.get("name") or grant.get("name") or slug),
                                  description=str(entry.get("description") or ""), skill_ids=skills, mcp_ids=mcps))
    return out

def plugin_skill_paths(ident, plugin):
    """[(skill_id, path, description)] of one plugin from task_plugins, read from its synced bundle."""
    import posixpath
    from personal_agent.workspace.constants import WORKSPACE
    dirs = (synced(ident, PLUGINS).get(plugin.slug) or {}).get("skills") or {}
    out = []
    for sid in plugin.skill_ids:
        sub = posixpath.normpath(str(dirs.get(sid) or ""))
        if not sub.startswith(SKILLS + "/") or ".." in sub.split("/"): continue
        rel = PLUGINS + "/" + plugin.slug + "/" + sub + "/SKILL.md"
        description = skill_description(ident, rel)
        if description is not None: out.append((sid, WORKSPACE + "/" + rel, description))
    return out

def platform_synced(ident, rel):
    """True for a workspace path the capability sync owns: a synced skill's directory or any plugin."""
    parts = rel.split("/")
    return (parts[0] == PLUGINS and len(parts) > 1) or (parts[0] == SKILLS and len(parts) > 1
            and (parts[1] == MANIFEST or parts[1] in synced(ident, SKILLS)))

def front_mcp_ids(config):
    """The main conversation only gets selected lookup tools; tasks get the full selection."""
    return [i for i in (config or {}).get("mcp_ids", []) if i in DEFAULT_MCP_IDS]
