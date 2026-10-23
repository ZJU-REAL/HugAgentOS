"""Schema and catalog initialization for the local CLI."""

def ensure_schema_and_seed():
    """create_all + built-in catalog seed (mirrors the app's startup hooks)."""
    from core.db.engine import SessionLocal, init_db
    from core.capabilities.mcp.service import prune_removed_builtin_mcp_servers, seed_builtin_mcp_servers_if_empty

    from features.installed import INSTALLED_FEATURES
    from core.harness.registry import load_models

    load_models(INSTALLED_FEATURES)
    from core.harness.registry.schema_lifecycle import load_schema_lifecycle
    load_schema_lifecycle(INSTALLED_FEATURES)
    init_db()
    db = SessionLocal()
    try:
        prune_removed_builtin_mcp_servers(db)
        seed_builtin_mcp_servers_if_empty(db)
    finally:
        db.close()
