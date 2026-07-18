"""Desktop client support endpoints — manifest distribution for Tauri auto-update + installer download.

The desktop client's (`desktop/`, Tauri v2) "one-click update" chain:

    client "check for updates" → GET  /v1/desktop/latest.json           # fetch update manifest
                               → if newer → GET /v1/desktop/download/{file}  # download installer
                               → local signature verification (pubkey) → install → restart

**Both endpoints must be public (no auth)**: the Tauri updater sends requests without a
session cookie. They only read static artifacts from the release directory, touch no user
data, and are safe to expose.

The release directory is set by the env var `DESKTOP_RELEASE_DIR` (default
`/app/desktop_release`). The release process (after `npm run build` on the Rust-equipped
Windows build machine) just puts three things into that directory:

    <DESKTOP_RELEASE_DIR>/
      ├─ latest.json                              # update manifest (format below)
      ├─ HugAgentOS_0.2.0_x64-setup.nsis.zip        # NSIS installer (updater artifact)
      └─ HugAgentOS_0.2.0_x64-setup.nsis.zip.sig    # matching signature (optional; the signature content can also be inlined into latest.json)

`latest.json` uses the Tauri v2 "dynamic manifest" format; `platforms.*.url` may be a **bare
filename** — this endpoint rewrites it into an absolute download URL based on the request
origin, decoupling it from the backend's actual domain/port (one latest.json works across
all environments):

    {
      "version": "0.2.0",
      "notes": "release notes for this update",
      "pub_date": "2026-07-16T00:00:00Z",
      "platforms": {
        "windows-x86_64": {
          "signature": "<contents of the .sig file>",
          "url": "HugAgentOS_0.2.0_x64-setup.nsis.zip"
        }
      }
    }
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from core.infra.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/desktop", tags=["Desktop"])


def _release_dir() -> Path:
    """Desktop release artifact dir (env override; default must match
    docker-compose.yml / .env.example / deploy_kit/publish_desktop.sh)."""
    return Path(os.getenv("DESKTOP_RELEASE_DIR", "/app/storage/desktop_release"))


def _download_base(request: Request) -> str:
    """Derive the absolute prefix for installer downloads from the request origin, decoupled from the backend's actual domain.

    Prefer the reverse-proxy-forwarded `X-Forwarded-*` (nginx scenario), falling back to request.base_url.
    """
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        base = f"{proto}://{host}"
    else:
        base = str(request.base_url).rstrip("/")
    # All backend /v1/* routes are exposed under the /api prefix via nginx; downloads use the same prefix.
    return f"{base.rstrip('/')}/api/v1/desktop/download"


@router.get("/latest.json", summary="桌面客户端更新清单（公开，供 Tauri updater 拉取）")
async def latest_manifest(request: Request) -> Response:
    """返回 Tauri v2 动态更新清单。

    - 发布目录/清单不存在 → 204（updater 视为「无可用更新」，静默不打扰）。
    - `platforms.*.url` 若是裸文件名/相对路径，改写为本机 download 接口的绝对地址。
    """
    manifest_path = _release_dir() / "latest.json"
    if not manifest_path.is_file():
        # No release configured → explicitly tell the updater "no update".
        return Response(status_code=204)

    try:
        manifest: Dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[desktop] latest.json 解析失败: %s", exc)
        return Response(status_code=204)

    base = _download_base(request)
    platforms = manifest.get("platforms")
    if isinstance(platforms, dict):
        for _key, spec in platforms.items():
            if not isinstance(spec, dict):
                continue
            url = spec.get("url")
            # Only rewrite bare filenames that are not absolute http(s); absolute URLs are kept as-is.
            if isinstance(url, str) and url and not url.lower().startswith(("http://", "https://")):
                spec["url"] = f"{base}/{url.lstrip('/')}"
            # signature may be inlined; if a .sig filename is given, read the content and inline it.
            sig = spec.get("signature")
            if isinstance(sig, str) and sig.endswith(".sig"):
                sig_path = _release_dir() / Path(sig).name
                if sig_path.is_file():
                    try:
                        spec["signature"] = sig_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass

    return JSONResponse(content=manifest)


@router.get("/download/{filename}", summary="桌面安装包下载（公开）")
async def download_installer(filename: str) -> Response:
    """按文件名从发布目录返回安装包。做严格的路径穿越防护。"""
    # Allow only the final filename; reject any path separators/traversal.
    safe_name = Path(filename).name
    if safe_name != filename or safe_name in ("", ".", ".."):
        return Response(status_code=404)

    file_path = _release_dir() / safe_name
    # Second layer of protection: after resolution the path must still be inside the release directory.
    try:
        release_root = _release_dir().resolve()
        resolved = file_path.resolve()
        resolved.relative_to(release_root)
    except (ValueError, OSError):
        return Response(status_code=404)

    if not resolved.is_file():
        return Response(status_code=404)

    return FileResponse(
        path=str(resolved),
        filename=safe_name,
        media_type="application/octet-stream",
    )
