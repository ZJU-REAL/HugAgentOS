"""Bind viewer tickets to the authenticated HTTP session, never iframe credentials."""
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from core.config.settings import settings

def capture(request):
    headers = {}
    for key in ("authorization", "x-desktop-bridge", "x-desktop-bridge-user"):
        value = request.headers.get(key)
        if value:
            headers[key] = value
    token = request.cookies.get(settings.session.cookie_name)
    if token:
        headers["cookie"] = settings.session.cookie_name + "=" + token
    return {"headers": headers, "path": request.url.path, "method": request.method}

async def validate(context, user_id, db):
    from core.auth.backend import get_current_user
    from core.config.local_mode import local_mode_enabled
    headers = context.get("headers", {})
    if not headers:
        # Local single-user mode and mock auth have no revocable credential.
        if local_mode_enabled() or settings.auth.mode == "mock":
            return
        raise HTTPException(401, "viewer_authentication_required")
    request = Request({"type": "http", "method": context.get("method", "POST"),
        "path": context.get("path", "/"), "query_string": b"",
        "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})
    authorization = headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    credentials = HTTPAuthorizationCredentials(scheme=scheme, credentials=value) if value else None
    current = await get_current_user(request, credentials=credentials, db=db)
    if str(current.user_id) != user_id:
        raise HTTPException(403, "viewer_identity_changed")
