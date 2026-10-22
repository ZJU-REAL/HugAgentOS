"""Workbench-specific binding and revision policy for generic plugin resources."""
from fastapi import HTTPException
from core.capabilities.plugins.resources.owners import execution_scope

def application_binding(db, user_id, key, install_id, revision, scope, module_id, selected=None):
    from features.workbench import catalog

    item = catalog.readable(db, user_id, key)
    if not item["availability"]["available"]:
        raise HTTPException(403, "application_unavailable")
    binding = item.get("binding", {})
    if (
        item["declaration"]["target"]["kind"] != "plugin"
        or binding.get("installId") != install_id
        or (
            binding.get("packageRevision") != revision
            and (selected is None or selected.revision != revision)
        )
        or scope != execution_scope()
        or (binding.get("origin") is not None and binding["origin"].get("kind") != scope)
    ):
        raise HTTPException(409, "resource_binding_mismatch")
    target = item["declaration"]["target"]
    if selected is not None and binding.get("packageRevision") != revision:
        previous = next(
            (
                a
                for a in (selected.ui.get("contributes") or {}).get("applications", [])
                if a["id"] == item["declaration"]["id"]
            ),
            None,
        )
        if previous is None:
            raise HTTPException(403, "resource_application_removed")
        target = previous["target"]
    allowed = {target.get("moduleId")} | {
        view.get("moduleId") for view in item["declaration"].get("views", [])
    }
    if module_id not in allowed:
        raise HTTPException(403, "resource_module_not_granted")
    return item



def resolve_installation(db, row, user_id):
    from features.workbench.installation_binding import exact
    selected = exact(db, row.slug, user_id, row.install_id)
    if selected.install_id != row.install_id:
        raise HTTPException(403, "resource_installation_mismatch")
    if selected.revision == row.revision:
        return selected
    try:
        from features.workbench.plugin_revisions import resolve_revision
    except ModuleNotFoundError as error:
        if error.name != "features.workbench.plugin_revisions":
            raise
        raise HTTPException(409, "revision_retention_unavailable") from None
    return resolve_revision(db, user_id, row.application_key, row.revision, row.instance_id)


def collect(db):
    from features.workbench.plugin_revisions import collect
    collect(db)
    from features.workbench.plugin_candidate_storage import janitor
    janitor(db)
