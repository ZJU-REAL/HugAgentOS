"""External target authentication. Secrets are accepted only by config admins."""

import secrets
from typing import Literal
from urllib.parse import urlencode

from api.deps import console_actor, require_config
from core.db.threads import run_in_session
from features.workbench import external_auth
from features.workbench.external_boundary import validate_cookie_scope
from core.auth.backend import UserContext, require_auth
from core.auth.session import find_session_by_hash
from core.config.settings import settings
from core.db.engine import get_db
from core.infra.logging import trace_id_var
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private(request):
            try:
                result = await handler(request)
                result.headers["Cache-Control"] = "no-store"
                result.headers["Referrer-Policy"] = "no-referrer"
                return result
            except RequestValidationError:
                raise HTTPException(
                    422,
                    detail={"code": "invalid_auth_request"},
                    headers={"Cache-Control": "no-store"},
                ) from None
            except HTTPException as error:
                error.headers = {
                    **(error.headers or {}),
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                }
                raise

        return private


router = APIRouter(tags=["workbench-auth"], route_class=PrivateRoute)


def browser_write(request: Request):
    if request.headers.get("content-type", "").split(";")[0].lower().strip() != "application/json":
        raise HTTPException(415, detail={"code": "json_required"})
    source = request.headers.get("origin")
    allowed = {
        str(request.base_url).rstrip("/"),
        *(value.strip() for value in settings.server.cors_origins.split(",") if value.strip()),
    }
    allowed.discard("*")
    if source and source not in allowed:
        raise HTTPException(403, detail={"code": "invalid_origin"})
    if request.headers.get("sec-fetch-site") == "cross-site" and source not in allowed:
        raise HTTPException(403, detail={"code": "invalid_origin"})


class AuthWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    mode: Literal["independent", "shared_identity", "exchange"]
    loginUrl: str = Field(default="", max_length=2048)
    identityProvider: str = Field(default="", max_length=200)
    audience: str = Field(default="", max_length=200)
    callback: str = Field(default="", max_length=2048)
    initiationUrl: str = Field(default="", max_length=2048)
    clientSecret: str | None = Field(default=None, min_length=32, max_length=256)
    ttlSeconds: int = Field(default=30, ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def mode_fields(self):
        if self.mode != "exchange" and (
            self.clientSecret or self.callback or self.audience or self.initiationUrl
        ):
            raise ValueError("unexpected exchange settings")
        if self.mode != "shared_identity" and (self.loginUrl or self.identityProvider):
            raise ValueError("unexpected identity settings")
        return self


class Exchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applicationKey: str = Field(max_length=160, min_length=1)
    ticket: str = Field(min_length=32, max_length=128)
    state: str = Field(min_length=32, max_length=128)
    audience: str = Field(min_length=1, max_length=200)
    callback: str = Field(min_length=1, max_length=2048)


@router.get("/admin/external-auth/{key}", dependencies=[Depends(require_config)])
def auth_config(key: str, db: Session = Depends(get_db)):
    external_auth.definition(db, key)
    return success_response(data=external_auth.read(db, key))


@router.put(
    "/admin/external-auth/{key}", dependencies=[Depends(require_config), Depends(browser_write)]
)
def update_auth(
    key: str, body: AuthWrite, actor: str = Depends(console_actor), db: Session = Depends(get_db)
):
    data = external_auth.save(db, key, body.model_dump(), actor, trace_id_var.get())
    return success_response(data=data)


@router.post("/external-auth/exchange", dependencies=[Depends(browser_write)])
async def exchange(body: Exchange, request: Request):
    auth = request.headers.get("authorization", "")
    credential = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
    data = await run_in_session(external_auth.redeem, body.model_dump(), credential)
    session_hash = data.pop("_sessionHash", None)
    if session_hash:
        session = await find_session_by_hash(session_hash)
        if not session or session.get("user_id") != data["subject"]:
            raise HTTPException(401, detail={"code": "invalid_launch_ticket"})
    return success_response(data=data)


class TargetChallenge(BaseModel):
    intent: str = Field(min_length=32, max_length=128)
    model_config = ConfigDict(extra="forbid")
    applicationKey: str = Field(max_length=160, min_length=1)
    state: str = Field(min_length=32, max_length=128)
    audience: str = Field(min_length=1, max_length=200)
    callback: str = Field(min_length=1, max_length=2048)


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge: str = Field(min_length=32, max_length=128)
    state: str = Field(min_length=32, max_length=128)


@router.post("/external-auth/challenges", dependencies=[Depends(browser_write)])
def challenge(body: TargetChallenge, request: Request, db: Session = Depends(get_db)):
    auth = request.headers.get("authorization", "")
    credential = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
    value = external_auth.challenge(db, body.model_dump(), credential)
    path = request.url.path.rsplit("/", 1)[0] + "/continue"
    url = str(
        request.url.replace(
            path=path, query="", fragment=urlencode({"challenge": value, "state": body.state})
        )
    )
    return success_response(data={"continueUrl": url})


@router.post("/external-auth/authorize", dependencies=[Depends(browser_write)])
def authorize(
    body: Authorization,
    request: Request,
    user: UserContext = Depends(require_auth()),
    db: Session = Depends(get_db),
):
    cookie = request.cookies.get(settings.session.cookie_name)
    url = external_auth.authorize(
        db,
        user.user_id,
        body.model_dump(),
        external_auth.digest(cookie) if cookie else None,
        lambda target, config: validate_cookie_scope(
            request, target, config, settings.session.cookie_domain
        ),
    )
    return success_response(data={"url": url})


@router.get("/external-auth/continue")
def continue_login(request: Request):
    # This fixed host document contains no third-party code. A fragment never
    # reaches HTTP access logs; JSON writes remain same-origin and authenticated.
    nonce = secrets.token_urlsafe(24)
    endpoint = request.url.path.rsplit("/", 1)[0] + "/authorize"
    import json

    html = f"""<!doctype html><html><body><p id="status">Authenticating…</p>
<script nonce="{nonce}">
const values=new URLSearchParams(location.hash.slice(1));
history.replaceState(null,'',location.pathname);
fetch({json.dumps(endpoint)},{{method:'POST',credentials:'same-origin',
headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{
challenge:values.get('challenge'),state:values.get('state')}})}})
.then(async response=>{{const result=await response.json();if(!response.ok)throw Error(result.detail?.code||'login_failed');
location.replace(result.data.url);}})
.catch(error=>{{document.getElementById('status').textContent=error.message==='launch_identity_mismatch'?'Host account or session changed. Reopen the application.':'Login failed. Close this window and try again.';}});
</script></body></html>"""
    return Response(
        html,
        media_type="text/html",
        headers={
            "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )
