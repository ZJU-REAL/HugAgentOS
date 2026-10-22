from features.desktop.api import desktop_capability
"""Isolated E2E gateway: real production HTTP route, synthetic authorization/upstream."""
from features.desktop.api import capability_dependencies
from features.desktop.api import desktop_capability_state
import sys
from pathlib import Path
from types import ModuleType

# Load just the production router, without api.__init__ starting the entire app.
root = Path(__file__).resolve().parents[3]
for name, relative in [("api", "api"), ("api.routes", "api/routes"),
                       ("api.routes.v1", "api/routes/v1")]:
    package = ModuleType(name)
    package.__path__ = [str(root / relative)]
    sys.modules[name] = package

import uvicorn
from fastapi import FastAPI

import core.db.models  # Initialize model extensions before standalone route imports.
from features.desktop.api import desktop_capability as bridge
from features.desktop.capability import service as desktop_capability
from features.desktop.gateway import observability as desktop_gateway_observability

# Replace account/config storage only. HTTP forwarding, pooling, SSE and error handling
# execute the production code; there are no real accounts, secrets or databases here.
desktop_capability.resolve_model_gateway_target = lambda *_: {
    "path": "responses",
    "url": "http://198.18.0.1:18091/responses",
    "model_name": "egress-fixture",
    "provider_type": "chat",
    "api_key": "synthetic-e2e-key",
}
capability_dependencies.stream_secrets = lambda *_: {"synthetic-e2e-key"}
desktop_gateway_observability.submit = lambda *_: None
app = FastAPI()
app.dependency_overrides[capability_dependencies.require_capability_user] = lambda: "e2e-user"
app.include_router(desktop_capability.router)


@app.get("/health")
def health():
    return {"ready": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=18092, log_level="warning")
