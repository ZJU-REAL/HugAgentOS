"""Public site hosting routes — static files + per-site dynamic APIs (KV / form collection).

- ``GET /site/{slug}/{path}``: static hosting. nginx ``location /site/``
  reverse-proxies it as-is.
- ``/site/{slug}/__api/kv/*``, ``/site/{slug}/__api/forms/*``: lightweight
  backend capabilities available to in-site JS (a minimal subset benchmarked
  against ChatGPT Sites' D1/R2). ``__api/`` is a reserved publish prefix
  (the service layer refuses to publish files by that name); these routes are
  declared before the catch-all so they match first.

Security:
- Public site responses carry ``Content-Security-Policy: sandbox`` (without
  allow-same-origin) — the document runs on an opaque origin, so in-site
  scripts cannot call the platform /api with the user's cookies; paired with
  ``Access-Control-Allow-Origin: *`` + OPTIONS preflight so fetch/ES modules
  work under an opaque origin.
- private / team sites are visible only to the site owner / team members
  (session-cookie check) and get no sandbox (otherwise sub-resource requests
  without cookies would all 403).
- Site API write operations have in-process rate limiting (per ip+slug) and
  quotas (service layer).
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from core.db.engine import get_db
from core.db.repository import SiteRepository
from core.infra.exceptions import AppException
from core.services.site_service import SiteService

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

# ── Simple in-process rate limiting (site API writes): 60 requests/minute per (ip, slug) ──
_RATE_LIMIT_PER_MIN = 60
_rate_buckets: dict[str, tuple[float, int]] = {}


def _rate_limit_write(ip: str, slug: str) -> None:
    now = time.monotonic()
    key = f"{ip}|{slug}"
    start, count = _rate_buckets.get(key, (now, 0))
    if now - start >= 60.0:
        start, count = now, 0
    count += 1
    _rate_buckets[key] = (start, count)
    if len(_rate_buckets) > 10000:  # guard against memory bloat
        _rate_buckets.clear()
    if count > _RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="操作太频繁，请稍后再试")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "")[:45]


def _common_headers(content_type: str, *, public_site: bool) -> dict:
    is_html = content_type.startswith("text/html")
    headers = {
        # Site content stays out of search engines
        "X-Robots-Tag": "noindex, nofollow",
        # HTML revalidates every time (new publishes take effect immediately); static assets get a short cache
        "Cache-Control": "no-cache" if is_html else "public, max-age=300",
        "X-Content-Type-Options": "nosniff",
    }
    if public_site:
        headers["Content-Security-Policy"] = _PUBLIC_CSP
        # After sandboxing the document is an opaque origin; in-site
        # fetch/XHR/ES-module requests for the site's own resources are treated
        # as cross-origin — open up CORS (the content is public anyway, and *
        # disallows sending credentials).
        headers["Access-Control-Allow-Origin"] = "*"
    return headers


async def _load_authorized_site(slug: str, request: Request, db: Session):
    """Fetch the site by slug and authorize visibility; unauthorized always 404 (don't leak existence)."""
    site = SiteRepository(db).get_by_slug(slug)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    if site.visibility != "public":
        from api.deps import _resolve_session_user_id

        user_id = await _resolve_session_user_id(request)
        if not SiteService(db).authorize_view(site, user_id):
            raise HTTPException(status_code=404, detail="Site not found")
    return site


def _api_json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status_code, headers=_CORS_API_HEADERS)


# ── Site dynamic APIs (declared before the catch-all) ─────────────

@router.options("/{slug}/__api/{rest:path}", include_in_schema=False)
async def site_api_preflight(slug: str, rest: str):
    """CORS preflight：opaque origin 下带 JSON body 的 fetch 会先发 OPTIONS。"""
    return Response(status_code=204, headers=_CORS_API_HEADERS)


@router.get("/{slug}/__api/kv/{key}", summary="站点 KV 读")
async def site_kv_get(
    slug: str, key: str, request: Request, db: Session = Depends(get_db),
):
    site = await _load_authorized_site(slug, request, db)
    try:
        value = SiteService(db).kv_get(site, key)
    except AppException as exc:
        return _api_json({"error": exc.message}, 400)
    if value is None:
        return _api_json({"key": key, "value": None, "exists": False}, 404)
    return _api_json({"key": key, "value": value, "exists": True})


@router.put("/{slug}/__api/kv/{key}", summary="站点 KV 写")
@router.post("/{slug}/__api/kv/{key}", include_in_schema=False)
async def site_kv_set(
    slug: str, key: str, request: Request, db: Session = Depends(get_db),
):
    site = await _load_authorized_site(slug, request, db)
    _rate_limit_write(_client_ip(request), slug)
    try:
        body = await request.json()
    except Exception:
        return _api_json({"error": "请求体必须是 JSON，如 {\"value\": \"...\"}"}, 400)
    value = body.get("value") if isinstance(body, dict) else None
    if value is None:
        return _api_json({"error": "缺少 value 字段"}, 400)
    import json as _json

    raw = value if isinstance(value, str) else _json.dumps(value, ensure_ascii=False)
    try:
        SiteService(db).kv_set(site, key, raw)
    except AppException as exc:
        return _api_json({"error": exc.message}, 400)
    return _api_json({"ok": True, "key": key})


@router.delete("/{slug}/__api/kv/{key}", summary="站点 KV 删")
async def site_kv_delete(
    slug: str, key: str, request: Request, db: Session = Depends(get_db),
):
    site = await _load_authorized_site(slug, request, db)
    _rate_limit_write(_client_ip(request), slug)
    try:
        deleted = SiteService(db).kv_delete(site, key)
    except AppException as exc:
        return _api_json({"error": exc.message}, 400)
    return _api_json({"ok": True, "deleted": deleted})


@router.post("/{slug}/__api/forms/{form_key}", summary="站点表单提交")
async def site_form_submit(
    slug: str, form_key: str, request: Request, db: Session = Depends(get_db),
):
    site = await _load_authorized_site(slug, request, db)
    _rate_limit_write(_client_ip(request), slug)
    try:
        payload = await request.json()
    except Exception:
        return _api_json({"error": "请求体必须是 JSON 对象"}, 400)
    try:
        submission_id = SiteService(db).submit_form(
            site, form_key, payload, client_ip=_client_ip(request),
        )
    except AppException as exc:
        return _api_json({"error": exc.message}, 400)
    return _api_json({"ok": True, "id": submission_id}, 201)


# ── Static hosting ───────────────────────────────────────────────

@router.get("/{slug}", include_in_schema=False)
async def site_root(slug: str):
    """裸 slug 重定向到带尾斜杠的目录形式，保证站内相对路径解析正确。"""
    return RedirectResponse(url=f"/site/{slug}/", status_code=307)


@router.get("/{slug}/{path:path}", summary="站点静态托管")
async def serve_site_file(
    slug: str,
    path: str,
    request: Request,
    db: Session = Depends(get_db),
):
    site = await _load_authorized_site(slug, request, db)

    resolved = SiteService(db).resolve_site_file(site, path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found")

    content, content_type = resolved
    # View stats: count HTML pages only (asset files don't count)
    if content_type.startswith("text/html"):
        try:
            SiteRepository(db).increment_view(site.site_id)
        except Exception:  # noqa: BLE001 — counting failure must not affect access
            logger.debug("site view_count increment failed", exc_info=True)
    return Response(
        content=content,
        media_type=content_type,
        headers=_common_headers(
            content_type, public_site=(site.visibility == "public")
        ),
    )
