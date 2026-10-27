"""Fresh private HOME bootstrap for opt-in local-engine acceptance tests."""
import os
import json
import secrets
from pathlib import Path
import cli
home = Path(os.environ["HUGAGENT_HOME"])
if (home / "data.db").exists():
    raise RuntimeError("Acceptance tests require a fresh HOME")
cli.apply_local_env(3001)
from hugagent_cli.schema import ensure_schema_and_seed
ensure_schema_and_seed()
uid = cli.bootstrap_admin("acceptance", secrets.token_urlsafe(32))
extra = json.loads(os.environ.get("HUGAGENT_MODEL_EXTRA_CONFIG", "{}"))
if not isinstance(extra, dict):
    raise ValueError("Model extra configuration must be an object")
cli.configure_model(os.environ["OPENAI_BASE_URL"], os.environ["HUGAGENT_MODEL_KEY"],
                    os.environ["HUGAGENT_MODEL"], test=False, context_length=extra.get("context_length", 131072))
cli.mark_web_onboarding_complete(uid)

if extra:
    from core.db.engine import SessionLocal
    from core.models.repository import list_providers, update_provider
    with SessionLocal() as db:
        for provider in list_providers(db):
            if provider.model_name == os.environ["HUGAGENT_MODEL"]:
                update_provider(db, provider.provider_id, extra_config=extra)
