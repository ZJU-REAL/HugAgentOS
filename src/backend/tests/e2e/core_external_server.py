"""Opt-in isolated API host for real configured-model and Cube verification.

Read credentials from private temporary files supplied by the operator. Never
reads business sessions or runs production startup workers.
"""

import json
import os
from pathlib import Path

from tests.e2e.core_refactor_server import build_app as build_isolated_app


def build_app():
    app = build_isolated_app()
    from core.db.engine import SessionLocal
    from core.db.model_repository import create_provider, assign_role
    from core.services.model_config import ModelConfigService

    path = Path(os.environ["CORE_E2E_MODEL_CONFIG"])
    assert path.is_relative_to("/tmp"), "Use a private disposable config file"
    assert path.stat().st_mode & 0o077 == 0, "Credentials must be owner-only"
    cfg = json.loads(path.read_text())
    with SessionLocal() as db:
        row = create_provider(
            db,
            display_name="Isolated real-model E2E",
            provider_type="chat",
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model_name=cfg["model_name"],
            extra_config={
                **cfg.get("extra", {}),
                "provider": cfg["provider"],
                "provider_extra": cfg.get("provider_extra", {}),
                "context_length": cfg["context_length"] or 32768,
            },
        )
        assign_role(db, "main_agent", row.provider_id)
        from core.db.models import UserShadow

        owner = db.get(UserShadow, "core-owner")
        owner.extra_data = {**(owner.extra_data or {}), "can_import_plugin": True}
        db.commit()
    ModelConfigService.get_instance().invalidate_cache()
    return app
