"""CE table creation and upgrade reconciliation."""

_CE_BOOTSTRAP_SERVER_DEFAULTS = {
    ("admin_skills", "dep_status"): "ready",
    ("user_agents", "plugin_ids"): "[]",
}


def _ce_metadata():
    """Build metadata with foreign keys to physically omitted tables removed."""
    import core.db.models  # noqa: F401
    from core.db.engine import Base
    from sqlalchemy import MetaData

    clone = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(clone)

    present = set(clone.tables)
    for table in clone.tables.values():
        for constraint in list(table.foreign_key_constraints):
            targets = {element.target_fullname.rsplit(".", 1)[0] for element in constraint.elements}
            if targets <= present:
                continue
            table.constraints.discard(constraint)
            for element in constraint.elements:
                element.parent.foreign_keys.discard(element)
                table.foreign_keys.discard(element)
    return clone


def ce_create_all(bind) -> list[str]:
    """Create the CE metadata and remove foreign keys to omitted edition tables."""
    clone = _ce_metadata()
    clone.create_all(bind=bind)
    return sorted(clone.tables)


def ce_reconcile_schema(bind) -> dict[str, list[str]]:
    """Idempotently add CE tables, columns, and indexes missing from an old database."""
    from core.db.schema_reconcile import reconcile_metadata_schema

    return reconcile_metadata_schema(
        bind,
        _ce_metadata(),
        bootstrap_server_defaults=_CE_BOOTSTRAP_SERVER_DEFAULTS,
    )
