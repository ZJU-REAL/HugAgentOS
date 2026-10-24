"""Pure authorized dependency closure; no catalog or database access."""

from core.projects.environment.config import FIELDS


def expand_environment(config, graph):
    """Resolve cycles safely; never grant a dependency absent from the user's graph."""
    selected = {
        field: list(config.get(field) or []) if config["mode"] == "custom" else list(graph[field])
        for field in FIELDS
    }
    effective = {field: [] for field in FIELDS}
    dependencies, unavailable = [], []
    visited = set()

    pending = [(field, ident, None) for field in FIELDS for ident in selected[field]][::-1]
    while pending:
        field, ident, parent = pending.pop()
        key = (field, ident)
        if key in visited:
            continue
        visited.add(key)
        item = graph[field].get(ident)
        if item is None:
            unavailable.append({"kind": field, "id": ident, "required_by": parent})
            continue
        effective[field].append(ident)
        if ident not in selected[field] and item.get("selectable", True):
            dependencies.append(
                {"kind": field, "id": ident, "name": item["name"], "required_by": parent}
            )
        children = [
            (dependency_field, dependency_id, ident)
            for dependency_field, ids in item.get("dependencies", {}).items()
            for dependency_id in ids
        ]
        # Preserve depth-first ordering without a recursion-depth limit.
        pending.extend(reversed(children))
    return {"effective": effective, "dependencies": dependencies, "unavailable": unavailable}
