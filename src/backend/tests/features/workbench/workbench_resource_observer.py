"""Install an isolated test-only backend observer into a real authenticated app."""

from urllib.parse import urlsplit

import httpx
from features.workbench.diagnostic_resources import snapshot
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.infra.responses import success_response
from core.infra.local_runtime.runner_auth import auth_headers
from fastapi import Depends, HTTPException, Response
from tests.features.workbench.workbench_observer_paths import guard_cli_binding, guard_environment, require_loopback


def install(app):
    guard_environment("STORAGE_PATH")
    guard_cli_binding()

    @app.get("/workbench-test/me/resource-metrics", dependencies=[Depends(require_loopback)])
    async def observe(
        response: Response, user: UserContext = Depends(require_auth()), db=Depends(get_db)
    ):
        response.headers["Cache-Control"] = "private, no-store"
        from core.sandbox.factory import get_sandbox_provider

        provider = get_sandbox_provider()
        value = snapshot(db, user.user_id)
        ordinary = getattr(provider, "_ordinary", provider)
        if ordinary.name != "script_runner":
            return success_response(data=value)
        url = ordinary._base_url
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or parts.hostname not in {"127.0.0.1", "::1"}
            or parts.username
            or parts.password
        ):
            raise HTTPException(503, detail={"code": "isolated_observer_unavailable"})
        try:
            async with httpx.AsyncClient(
                timeout=3, trust_env=False, follow_redirects=False, headers=auth_headers()
            ) as client:
                result = await client.post(
                    url.rstrip("/") + "/workbench-test/process-observations",
                    json={"user_id": user.user_id},
                )
            if result.status_code != 200 or len(result.content) > 16384:
                raise ValueError()
            raw = result.json()
            if (
                raw.get("provider") != "script_runner"
                or type(raw.get("complete")) is not bool
                or raw.get("observation") != "actual_local_process_handles"
            ):
                raise ValueError()
            keys = ("appOwnerRunningWorkers", "appOwnerStartingWorkers", "unknownWorkers")
            if any(type(raw.get(key)) is not int or not 0 <= raw[key] <= 64 for key in keys):
                raise ValueError()
            if sum(raw[key] for key in keys) > 64 or (
                raw["complete"] and raw["unknownWorkers"] != 0
            ):
                raise ValueError()
            value["runtime"] = {
                key: raw[key] for key in ("provider", "complete", "observation", *keys)
            }
            return success_response(data=value)
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, detail={"code": "isolated_observer_unavailable"}) from None
