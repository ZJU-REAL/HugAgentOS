#!/usr/bin/env python3
"""
Export release notes and capability-center data for cross-environment migration.

Exported content:
  1. Release notes (content_blocks: docs_updates + docs_capabilities)
  2. Capability center (catalog.json + catalog_overrides)

Usage:
    # Export from a running backend API (recommended)
    python scripts/export_content.py --api-url http://localhost:3000/api

    # Export directly from the database
    python scripts/export_content.py --database-url postgresql://user:pass@host/db

    # Specify the output directory
    python scripts/export_content.py --api-url http://localhost:3000/api -o /tmp/export

    # Export release notes only
    python scripts/export_content.py --api-url http://localhost:3000/api --only docs

    # Export the capability center only
    python scripts/export_content.py --api-url http://localhost:3000/api --only catalog

    # Additionally export the system prompt version pool (prompt_versions: system/code_exec/distillation/plan_mode)
    python scripts/export_content.py --api-url http://localhost:3000/api --include-prompts
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_DIR.parent          # src/backend
CATALOG_JSON = BACKEND_ROOT / "core" / "config" / "catalog.json"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "exported"


def _load_env_token() -> str:
    """Try to read ADMIN_TOKEN from .env files."""
    for candidate in [
        BACKEND_ROOT.parent.parent / ".env",   # project root
        BACKEND_ROOT / ".env",
    ]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line.startswith("ADMIN_TOKEN"):
                    _, _, val = line.partition("=")
                    return val.strip().strip('"').strip("'")
    return os.getenv("ADMIN_TOKEN", "")


# ---------------------------------------------------------------------------
# API-based export
# ---------------------------------------------------------------------------

def export_docs_via_api(api_url: str, token: str) -> dict:
    """GET /v1/content/docs/export"""
    import urllib.request
    import urllib.error

    url = f"{api_url.rstrip('/')}/v1/content/docs/export"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"✗ 导出版本说明失败: HTTP {exc.code} — {exc.read().decode()}")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"✗ 无法连接后端: {exc.reason}")
        sys.exit(1)

    return body.get("data", body)


def export_prompts_via_api(api_url: str, token: str) -> dict:
    """GET /v1/content/prompts/export"""
    import urllib.request
    import urllib.error

    url = f"{api_url.rstrip('/')}/v1/content/prompts/export"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"✗ 导出系统提示词失败: HTTP {exc.code} — {exc.read().decode()}")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"✗ 无法连接后端: {exc.reason}")
        sys.exit(1)

    return body.get("data", body)


def export_catalog_via_api(api_url: str) -> dict:
    """GET /v1/catalog (public, no auth needed)."""
    import urllib.request
    import urllib.error

    url = f"{api_url.rstrip('/')}/v1/catalog"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"✗ 导出能力中心失败: {exc}")
        sys.exit(1)

    return body.get("data", body)


# ---------------------------------------------------------------------------
# DB-based export (offline, no running server needed)
# ---------------------------------------------------------------------------

def export_docs_via_db(database_url: str) -> dict:
    sys.path.insert(0, str(BACKEND_ROOT))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.db.models import ContentBlock
    from core.content.content_blocks import build_docs_snapshot

    engine = create_engine(database_url)
    session = sessionmaker(bind=engine)()
    try:
        return build_docs_snapshot(session)
    finally:
        session.close()


def export_prompts_via_db(database_url: str) -> dict:
    sys.path.insert(0, str(BACKEND_ROOT))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.content.content_blocks import build_prompt_snapshot

    engine = create_engine(database_url)
    session = sessionmaker(bind=engine)()
    try:
        return build_prompt_snapshot(session)
    finally:
        session.close()


def export_catalog_overrides_via_db(database_url: str) -> list:
    sys.path.insert(0, str(BACKEND_ROOT))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.db.models import CatalogOverride

    engine = create_engine(database_url)
    session = sessionmaker(bind=engine)()
    try:
        rows = session.query(CatalogOverride).all()
        return [
            {
                "user_id": r.user_id,
                "kind": r.kind,
                "item_id": r.item_id,
                "enabled": r.enabled,
                "config_data": r.config_data or {},
            }
            for r in rows
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  → {path}  ({path.stat().st_size:,} bytes)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="导出版本说明与能力中心数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--api-url", help="后端 API 地址，如 http://localhost:3000/api")
    source.add_argument("--database-url", help="直接连接数据库（无需运行中的后端）")

    parser.add_argument("--admin-token", help="ADMIN_TOKEN（默认从 .env 读取）")
    parser.add_argument("-o", "--output-dir", help="输出目录（默认 scripts/exported/）")
    parser.add_argument(
        "--only",
        choices=["docs", "catalog", "prompts"],
        help="仅导出指定部分（--only prompts 等价于只导出提示词）",
    )
    parser.add_argument(
        "--include-prompts",
        action="store_true",
        help="额外导出系统提示词 + 提示词广场到 prompts_snapshot_*.json",
    )

    args = parser.parse_args()

    token = args.admin_token or _load_env_token()
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 50)
    print("HugAgentOS 数据导出")
    print("=" * 50)

    # ── 1. Release notes ─────────────────────────────────────────────────
    if args.only in (None, "docs"):
        print("\n[1/2] 导出版本说明 (content_blocks) ...")
        if args.api_url:
            if not token:
                print("✗ 需要 ADMIN_TOKEN，请通过 --admin-token 指定或在 .env 中设置")
                sys.exit(1)
            docs_snapshot = export_docs_via_api(args.api_url, token)
        else:
            docs_snapshot = export_docs_via_db(args.database_url)

        docs_file = output_dir / f"docs_snapshot_{timestamp}.json"
        _write_json(docs_file, docs_snapshot)

        # Statistics
        blocks = docs_snapshot.get("blocks", {})
        for name, block in blocks.items():
            count = len(block.get("payload", []))
            print(f"     {name}: {count} 条")

    # ── 2. Capability center ─────────────────────────────────────────────
    if args.only in (None, "catalog"):
        print("\n[2/2] 导出能力中心 (catalog) ...")

        catalog_export = {}

        # 2a. catalog.json (static definitions)
        if CATALOG_JSON.exists():
            catalog_export["catalog_json"] = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
            for kind in ("skills", "agents", "mcp", "kb"):
                items = catalog_export["catalog_json"].get(kind, [])
                print(f"     catalog.json → {kind}: {len(items)} 项")
        else:
            print(f"  ⚠ catalog.json 不存在: {CATALOG_JSON}")

        # 2b. catalog_overrides (per-user personalization overrides)
        if args.api_url:
            # The API has no endpoint for bulk-exporting overrides; only catalog.json is exported
            catalog_export["catalog_overrides"] = []
            print("     catalog_overrides: 跳过（API 模式不支持批量导出用户覆盖）")
        else:
            overrides = export_catalog_overrides_via_db(args.database_url)
            catalog_export["catalog_overrides"] = overrides
            print(f"     catalog_overrides: {len(overrides)} 条")

        catalog_export["exported_at"] = datetime.utcnow().isoformat() + "Z"

        catalog_file = output_dir / f"catalog_snapshot_{timestamp}.json"
        _write_json(catalog_file, catalog_export)

    # ── 3. System prompts + prompt hub ───────────────────────────────────
    if args.include_prompts or args.only == "prompts":
        print("\n[+] 导出系统提示词 (prompt_versions) + 提示词广场 (prompt_hub) ...")
        if args.api_url:
            if not token:
                print("✗ 需要 ADMIN_TOKEN，请通过 --admin-token 指定或在 .env 中设置")
                sys.exit(1)
            prompt_snapshot = export_prompts_via_api(args.api_url, token)
        else:
            prompt_snapshot = export_prompts_via_db(args.database_url)

        prompt_file = output_dir / f"prompts_snapshot_{timestamp}.json"
        _write_json(prompt_file, prompt_snapshot)

        blocks = prompt_snapshot.get("blocks", {})

        # prompt_versions: version count per kind + active version
        pv_payload = blocks.get("prompt_versions", {}).get("payload", {})
        versions = pv_payload.get("versions", []) if isinstance(pv_payload, dict) else []
        active = pv_payload.get("active", {}) if isinstance(pv_payload, dict) else {}
        by_kind: dict = {}
        for v in versions:
            by_kind.setdefault(v.get("kind", "?"), []).append(v.get("id"))
        for kind, ids in sorted(by_kind.items()):
            print(f"     prompt_versions/{kind}: {len(ids)} 个版本（激活: {active.get(kind)}）")

        # prompt_hub: template count
        hub_payload = blocks.get("prompt_hub", {}).get("payload", [])
        hub_count = len(hub_payload) if isinstance(hub_payload, list) else 0
        print(f"     prompt_hub: {hub_count} 条提示词模板")

    print("\n" + "=" * 50)
    print("✓ 导出完成")
    print("=" * 50)


if __name__ == "__main__":
    main()
