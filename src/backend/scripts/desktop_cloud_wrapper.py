"""Test-only ASGI wrapper: serve the backend under ``/api`` the way nginx does in front of the cloud.

The desktop bridge always calls ``{cloud_base}/api/v1/...``; a real deployment's
nginx strips ``/api``. This wrapper strips it the same way and forwards the
lifespan scope untouched so the startup hooks run. Used by
``scripts/dual_mode_e2e.py`` to stand up a "cloud" process without nginx:

    uvicorn scripts.desktop_cloud_wrapper:app --host 0.0.0.0 --port 3001
"""

from api.app import app as inner


async def _fixture_session(scope, receive, send):
    # This entry point is used only by the isolated E2E wrapper. Production
    # serves api.app directly and has neither this route nor its ephemeral key.
    import hmac, json, os
    from pathlib import Path
    from starlette.responses import JSONResponse

    key_path = Path("/tmp/capability-e2e-fixture.secret")
    headers = dict(scope.get("headers") or [])
    expected = key_path.read_text().strip() if key_path.is_file() else ""
    supplied = headers.get(b"authorization", b"").decode("ascii", errors="ignore")
    if (
        os.getenv("AUTH_MODE") != "mock"
        or not expected
        or not hmac.compare_digest(supplied, "Bearer " + expected)
    ):
        await JSONResponse({"detail": "not found"}, status_code=404)(scope, receive, send)
        return
    body = b""
    while True:
        part = await receive()
        body += part.get("body", b"")
        if len(body) > 4096:
            await JSONResponse({"detail": "too large"}, status_code=413)(scope, receive, send)
            return
        if not part.get("more_body"):
            break
    data = json.loads(body)
    from core.db.engine import SessionLocal
    from core.db.models import UserShadow
    from core.auth.session import create_session
    from core.config.settings import settings

    with SessionLocal() as db:
        user = (
            db.query(UserShadow).filter(UserShadow.user_center_id == data["user_center_id"]).first()
        )
        if user is None:
            await JSONResponse({"detail": "not found"}, status_code=404)(scope, receive, send)
            return
        payload = {
            "user_id": str(user.user_id),
            "user_center_id": user.user_center_id,
            "username": user.username,
        }
    session = await create_session(payload)
    await JSONResponse(
        {"cookie": settings.session.cookie_name, "token": session},
        headers={"Cache-Control": "no-store"},
    )(scope, receive, send)


async def app(scope, receive, send):
    if scope["type"] == "http":
        path = scope.get("path", "")
        if path == "/__e2e/session" and scope.get("method") == "POST":
            return await _fixture_session(scope, receive, send)
        if path == "/api" or path.startswith("/api/"):
            stripped = path[4:] or "/"
            scope = dict(scope)
            scope["path"] = stripped
            scope["raw_path"] = stripped.encode("utf-8")
    await inner(scope, receive, send)
