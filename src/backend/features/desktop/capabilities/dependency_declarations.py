"""Dependency declarations shared by capability inspectors."""

_FIELDS = {"skills": "skill", "agents": "agent", "mcp": "mcp", "plugins": "plugin"}
_AGENT_FIELDS = {
    "skill_ids": "skill",
    "mcp_server_ids": "mcp",
    "plugin_ids": "plugin",
    "kb_ids": "kb",
}


def _entries(definition, kind, depth=0):
    if depth > 32 or not isinstance(definition, dict):
        return [{"kind": "unknown", "id": "invalid-dependency-declaration"}]
    entries = []
    components = definition.get("components")
    if isinstance(components, dict):
        for group, values in components.items():
            for value in values if isinstance(values, list) else [values]:
                entry = dict(value) if isinstance(value, dict) else {"id": value}
                entry.setdefault("kind", _FIELDS.get(group, group))
                entries.append(entry)
    for group, dep_kind in _AGENT_FIELDS.items():
        values = definition.get(group) or []
        if isinstance(values, str):
            values = values.replace(",", " ").split()
        for value in values:
            entry = dict(value) if isinstance(value, dict) else {"id": value}
            entry.setdefault("kind", dep_kind)
            entries.append(entry)
    mcp_names = definition.get("mcp_servers") or definition.get("mcp-server-ids")
    if mcp_names:
        for name in (
            mcp_names if isinstance(mcp_names, list) else str(mcp_names).replace(",", " ").split()
        ):
            entries.append({"kind": "mcp", "id": name})
    if definition.get("model_provider_id"):
        entries.append({"kind": "model", "id": definition["model_provider_id"]})
    dependencies = definition.get("dependencies") or []
    if isinstance(dependencies, list):
        entries.extend(
            dict(value) if isinstance(value, dict) else {"kind": "unknown", "id": value}
            for value in dependencies
        )
    elif isinstance(dependencies, dict):
        for dep_kind, values in dependencies.items():
            if dep_kind == "warnings":
                continue
            for value in values if isinstance(values, list) else [values]:
                entry = dict(value) if isinstance(value, dict) else {"id": value}
                entry.setdefault("kind", dep_kind)
                entries.append(entry)
    else:
        entries.append({"kind": "unknown", "id": "invalid-dependencies"})
    extra = definition.get("extra_config") or {}
    if isinstance(extra, dict) and extra.get("capability_requirements"):
        requirements = extra["capability_requirements"]
        if isinstance(requirements, list):
            requirements = {"dependencies": requirements}
        entries += _entries(requirements, kind, depth + 1)
    raw_extensions = definition.get("extensions") or []
    if isinstance(raw_extensions, dict):
        extensions = []
        for key, value in raw_extensions.items():
            if key in (
                "architecture",
                "python_version",
                "node_version",
                "execution_plane",
                "platforms",
            ):
                continue  # Inspected by platform_ok below, never assumed satisfied.
            item = dict(value) if isinstance(value, dict) else {"id": key}
            item.setdefault("id", key)
            extensions.append(item)
    else:
        extensions = list(raw_extensions)
    for key in ("hooks", "rules", "commands"):
        values = definition.get(key)
        if values:
            for value in values if isinstance(values, list) else [values]:
                item = dict(value) if isinstance(value, dict) else {"id": key}
                extensions.append(item)
    for extension in extensions:
        value = dict(extension) if isinstance(extension, dict) else {"id": extension}
        entries.append({**value, "kind": "unsupported_extension"})
    return entries


def _identifier(entry):
    return str(
        entry.get("id")
        or entry.get("key")
        or entry.get("skill_id")
        or entry.get("agent_id")
        or entry.get("server_id")
        or ""
    )
