"""Public site hosting routes — static files and site access authorization.

- ``GET /site/{slug}/{path}``: static hosting. nginx ``location /site/``
  reverse-proxies it as-is.

Security:
- Public site responses carry ``Content-Security-Policy: sandbox`` (without
  allow-same-origin) — the document runs on an opaque origin, so in-site
  scripts cannot call the platform /api with the user's cookies; paired with
  ``Access-Control-Allow-Origin: *`` + OPTIONS preflight so fetch/ES modules
  work under an opaque origin.
- private / team sites are visible only to the site owner / team members
  (session-cookie check) and get no sandbox (otherwise sub-resource requests
  without cookies would all 403).
- 设了访问密码的站点，静态内容在解锁前一律返回统一验证页，且不套 sandbox——
  否则文档落在不透明源上，子资源请求带不上解锁 cookie，会全部 401（见
  ``_sandboxed``）。两道闸都由 ``_load_authorized_site`` 统一把守，密码闸默认开着；
  ``require_unlock=False`` 是写在各个 ``__api/*`` 处理器上的显式豁免（理由见该函数的
  docstring）。
- Site API write operations have in-process rate limiting (per ip+slug) and
  quotas (service layer).
"""

import logging
from typing import Optional

from api.routes.site_gate import render_site_gate
from core.db.engine import get_db
from core.db.repository import SiteRepository
from core.services.site_password import (
    ACCESS_COOKIE_NAME,
    access_cookie_params,
    check_access_password,
    issue_access_token,
    site_base_path,
    verify_access_token,
)
from core.services.site_service import SiteService
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/site", tags=["sites-serve"])

_PUBLIC_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-modals "
    "allow-pointer-lock allow-downloads"
)

_CORS_API_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
    "Cache-Control": "no-store",
}

def _client_ip(request: Request) -> str:
    # X-Real-IP 由 nginx 按连接对端填写，访客改不了；X-Forwarded-For 的头一段是客户端
    # 自己塞的，拿它当限流 key 等于让攻击者随手换一把新钥匙。
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real[:45]
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        # 退一步取最后一段——那是最靠近本服务的一跳填的。
        return fwd.split(",")[-1].strip()[:45]
    return (request.client.host if request.client else "")[:45]


def _sandboxed(site) -> bool:
    """站点内容是否套 sandbox CSP。

    沙箱把文档放到不透明源上，浏览器随之把它的子资源请求当跨站处理、一律不带
    SameSite cookie——设了访问密码的站点因此取不到解锁凭据，脚本样式会全部 401。
    所以密码闸和沙箱只能二选一，与 private / team 站点不套沙箱是同一个理由。
    """
    return site.visibility == "public" and not site.access_password_hash


def _common_headers(content_type: str, *, sandbox: bool, cache: Optional[str] = None) -> dict:
    is_html = content_type.startswith("text/html")
    headers = {
        # Site content stays out of search engines
        "X-Robots-Tag": "noindex, nofollow",
        # HTML revalidates every time (new publishes take effect immediately); static assets get a short cache
        "Cache-Control": cache or ("no-cache" if is_html else "public, max-age=300"),
        "X-Content-Type-Options": "nosniff",
    }
    if sandbox:
        headers["Content-Security-Policy"] = _PUBLIC_CSP
        # After sandboxing the document is an opaque origin; in-site
        # fetch/XHR/ES-module requests for the site's own resources are treated
        # as cross-origin — open up CORS (the content is public anyway, and *
        # disallows sending credentials).
        headers["Access-Control-Allow-Origin"] = "*"
    return headers


async def _load_authorized_site(
    slug: str, request: Request, db: Session, *, require_unlock: bool = True
):
    """取站点并鉴权，返回 ``(site, gate)``。

    两道闸：可见性（不通过一律 404，不泄露站点是否存在）与访问密码（不通过返回
    ``gate`` 响应，由调用方直接回给访客）。默认两道都过——``require_unlock=False``
    是显式豁免，只给 ``__api/*`` 用：站内脚本跑在沙箱的不透明源上、请求不带凭据，
    仅解锁入口 ``__api/access`` 可豁免；应用表单仍执行密码验证。
    """
    site = await run_in_threadpool(SiteRepository(db).get_by_slug, slug)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    if site.visibility != "public":
        from api.deps import _resolve_session_user_id

        user_id = await _resolve_session_user_id(request)
        if not await run_in_threadpool(SiteService(db).authorize_view, site, user_id):
            raise HTTPException(status_code=404, detail="Site not found")
    if not require_unlock or not site.access_password_hash:
        return site, None
    return site, await _unlock_gate(site, request, db)


async def _unlock_gate(site, request: Request, db: Session) -> Optional[Response]:
    """已解锁返回 None；否则返回验证页。凭据校验是纯 HMAC，不值得丢进线程池。"""
    if verify_access_token(site, request.cookies.get(ACCESS_COOKIE_NAME)):
        return None
    # 没有有效凭据才回落到会话身份——能管理该站点的人免密进入。
    from api.deps import _resolve_session_user_id

    user_id = await _resolve_session_user_id(request)
    if user_id and await run_in_threadpool(SiteService(db).authorize_access, site, user_id, None):
        # 给管理者也发一枚凭据，否则同一个页面的每个资源都要重跑一遍会话 + 权限查询。
        request.state.site_access_grant = site
        return None
    return _gate_response(site, request)


def _api_json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status_code, headers=_CORS_API_HEADERS)


# ── Site dynamic APIs (declared before the catch-all) ─────────────


@router.options("/{slug}/__api/{rest:path}", include_in_schema=False)
async def site_api_preflight(slug: str, rest: str):
    """CORS preflight：opaque origin 下带 JSON body 的 fetch 会先发 OPTIONS。"""
    return Response(status_code=204, headers=_CORS_API_HEADERS)


@router.post("/{slug}/__api/access", summary="站点访问密码校验")
async def site_unlock(slug: str, request: Request, db: Session = Depends(get_db)):
    """校验访问密码并下发解锁凭据（统一验证页提交到这里）。这是解锁入口，必须免闸。"""
    # 限流放在取站点之前：被挡下的尝试连一次 get_by_slug 都不该花。
    from core.services.site_rate_limit import count_attempt
    await count_attempt(_client_ip(request), slug, unlock=True)
    site, _ = await _load_authorized_site(slug, request, db, require_unlock=False)
    if not site.access_password_hash:
        return _api_json({"ok": True})
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = body.get("password") if isinstance(body, dict) else None
    if not await run_in_threadpool(check_access_password, site, password):
        return _api_json({"error": "密码不正确"}, 401)
    response = _api_json({"ok": True})
    response.set_cookie(value=issue_access_token(site), **access_cookie_params(slug))
    return response


# ── Static hosting ───────────────────────────────────────────────


@router.get("/{slug}", include_in_schema=False)
async def site_root(slug: str):
    """裸 slug 重定向到带尾斜杠的目录形式，保证站内相对路径解析正确。"""
    return RedirectResponse(url=site_base_path(slug), status_code=307)


@router.get("/{slug}/{path:path}", summary="站点静态托管")
async def serve_site_file(
    slug: str,
    path: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if path == "__api" or path.startswith("__api/"):
        raise HTTPException(404, "API route not found")
    site, gate = await _load_authorized_site(slug, request, db)
    if gate is not None:
        return gate
    response = await run_in_threadpool(_site_file_response, db, site, path)
    granted = getattr(request.state, "site_access_grant", None)
    if granted is not None:
        response.set_cookie(value=issue_access_token(granted), **access_cookie_params(slug))
    return response


def _gate_response(site, request: Request) -> Response:
    """统一验证页：不套 sandbox CSP，否则页面落在不透明源上、拿不到解锁 cookie。

    只有文档请求值得渲染整页；图片/脚本等子资源拿到一页 HTML 也只会被丢掉，回一个空 401。
    """
    headers = _common_headers("text/html", sandbox=False, cache="no-store")
    if request.headers.get("sec-fetch-dest", "document") != "document":
        return Response(status_code=401, headers=headers)
    return HTMLResponse(
        content=render_site_gate(
            site_base_path(site.slug), site.title, request.headers.get("accept-language", "")
        ),
        status_code=401,
        headers=headers,
    )


def _site_file_response(db: Session, site, path: str) -> Response:
    # Storage I/O, row updates and ORM refreshes all run off the event loop.
    resolved = SiteService(db).resolve_site_file(site, path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found")

    content, content_type = resolved
    # View stats: count HTML pages only (asset files don't count)
    if content_type.startswith("text/html"):
        try:
            SiteRepository(db).increment_view(site.site_id)
        except Exception:  # noqa: BLE001 — counting failure must not affect access
            db.rollback()
            logger.debug("site view_count increment failed", exc_info=True)
    return Response(
        content=content,
        media_type=content_type,
        headers=_common_headers(content_type, sandbox=_sandboxed(site)),
    )
