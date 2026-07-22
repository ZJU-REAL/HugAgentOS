"""CE table creation without enterprise model-name knowledge."""


def ce_create_all(bind) -> list[str]:
    """Create the CE metadata and remove foreign keys to omitted edition tables."""
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

    clone.create_all(bind=bind)
    return sorted(clone.tables)
