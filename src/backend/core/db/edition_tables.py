"""CE/EE table-creation boundary (single source of truth for migration baseline D1).

The ``core.db.models`` package is shared by both editions (defining the EE table
classes is harmless in itself), but CE must not create empty EE-only tables. This
module provides the set of EE-only table names, filtered by both table-creation
entry points:

- the CE branch of ``core.db.engine.init_db`` (SQLite startup fallback)
- the CE overlay migration baseline ``alembic/versions/ce_0001_initial.py``

EE (``JX_EDITION=ee``, covering all license states including internal/licensed)
always creates the full set of tables, matching historical behavior; filtering
happens only when ``JX_EDITION=ce``.

Maintenance rule: when adding an EE-only model, add its table name to
``EE_ONLY_TABLES`` at the same time. Every name in the set must actually exist in
the metadata (``ce_create_all`` asserts this), so a renamed model that is not
updated here cannot silently degrade into full-table creation.

Note a few tables that "look EE but are actually needed by CE" — do NOT add them
to the set:
``admin_prompt_parts`` (read at runtime by prompts/prompt_runtime),
``memory_sanitizer_rules`` (queried unconditionally by core/memory/sanitizer),
``admin_skills``/``admin_mcp_servers`` (personal self-service skills/MCP, owner-isolated),
``marketplace_submissions`` (CE marketplace.py keeps the submission endpoint).
"""

EE_ONLY_TABLES: frozenset[str] = frozenset({
    # Multi-tenant / SSO / invitations
    "teams",
    "team_members",
    "team_folders",
    "invite_codes",
    # Role/permission system (named capability bundles + assignment to teams/users) —
    # CE is single-tenant with no roles; resolution degrades to "personal → system"
    "roles",
    "role_assignments",
    # KB permission grants (per-user/team authorization; CE public KBs are always
    # visible to everyone, so this table is not created)
    "kb_grants",
    # Marketplace item visibility grants (scoped whitelist; CE has no admin console,
    # marketplace is always visible to everyone)
    "marketplace_visibility_grants",
    # Audit (user-facing audit + memory audit — CE's memory audit is a stub that
    # writes no table; /v1/memories/audit short-circuits under the
    # audit_enabled=False default)
    "audit_logs",
    "memory_audit",
    # Billing
    "model_pricing",
    # Data sources / metadata governance (DB connections, table/column metadata, Golden SQL)
    "data_sources",
    "ds_table_meta",
    "ds_column_meta",
    "ds_golden_sql",
    # External model gateway: virtual-key mirror (control plane, EE-only capability model_gateway)
    "gateway_virtual_keys",
    # Persistent sandbox rebuild / skill distillation (admin-console drafts + distillation run records)
    "sandbox_rebuilds",
    "admin_skill_drafts",
    "distillation_runs",
})


def ce_create_all(bind) -> list[str]:
    """Create all non-EE tables under CE and return the list of created table names (idempotent, dialect-aware).

    Creates tables on a cloned MetaData: cross-boundary FK constraints in CE tables
    that point at EE tables (projects/artifacts → teams/team_folders, plan D3
    "keep the column, always NULL") would make PostgreSQL fail table creation
    because the referenced tables don't exist — so those constraints are stripped
    on the clone (the columns themselves remain), the original metadata is
    untouched, and ORM mappings are unaffected.
    """
    import core.db.models  # noqa: F401  registers all ORM tables into metadata
    from sqlalchemy import MetaData

    from core.db.engine import Base

    metadata = Base.metadata
    missing = EE_ONLY_TABLES - set(metadata.tables)
    if missing:
        raise RuntimeError(
            f"EE_ONLY_TABLES 含 metadata 中不存在的表（模型改名漏同步？）: {sorted(missing)}"
        )
    clone = MetaData()
    for name, table in metadata.tables.items():
        if name not in EE_ONLY_TABLES:
            table.to_metadata(clone)
    for table in clone.tables.values():
        for fkc in list(table.foreign_key_constraints):
            referred = {
                elem.target_fullname.rsplit(".", 1)[0] for elem in fkc.elements
            }
            if referred & EE_ONLY_TABLES:
                # Both the constraints set and the column-level ForeignKey elements
                # must be removed — foreign_key_constraints / DDL ordering read the latter
                table.constraints.discard(fkc)
                for elem in fkc.elements:
                    elem.parent.foreign_keys.discard(elem)
                    table.foreign_keys.discard(elem)
    clone.create_all(bind=bind)
    return sorted(clone.tables)
