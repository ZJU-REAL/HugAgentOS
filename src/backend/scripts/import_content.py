#!/usr/bin/env python3
"""
Import release-notes and capability-center data, for cross-environment migration.

Used together with export_content.py to import development-environment data into
production.

Usage:
    # Import release notes via the API (recommended)
    python scripts/import_content.py --api-url http://<PROD_HOST>/api \
        --docs docs_snapshot_20260310_143000.json

    # Import the capability center via the API (catalog.json is overwritten; backend restart required)
    python scripts/import_content.py --api-url http://<PROD_HOST>/api \
        --catalog catalog_snapshot_20260310_143000.json

    # Import both at once
    python scripts/import_content.py --api-url http://<PROD_HOST>/api \
        --docs docs_snapshot.json --catalog catalog_snapshot.json

    # Write directly to the database (no running backend needed)
    python scripts/import_content.py --database-url postgresql://user:pass@host/db \
        --docs docs_snapshot.json --catalog catalog_snapshot.json

    # Import the system prompt version pool (prompt_versions: system/code_exec/distillation/plan_mode)
    python scripts/import_content.py --api-url http://<PROD_HOST>/api \
        --prompts prompts_snapshot_20260310_143000.json

    # Do not overwrite existing data (only write missing blocks)
    python scripts/import_content.py --api-url http://<PROD_HOST>/api \
        --docs docs_snapshot.json --no-overwrite

    # Dry run, no actual writes
    python scripts/import_content.py --api-url http://<PROD_HOST>/api \
        --docs docs_snapshot.json --dry-run
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_DIR.parent
CATALOG_JSON = BACKEND_ROOT / "configs" / "catalog.json"


def _load_env_token() -> str:
    for candidate in [
        BACKEND_ROOT.parent.parent / ".env",
        BACKEND_ROOT / ".env",
    ]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line.startswith("ADMIN_TOKEN"):
                    _, _, val = line.partition("=")
                    return val.strip().strip('"').strip("'")
    return os.getenv("ADMIN_TOKEN", "")


def _read_json(path: Path) -> dict:
    if not path.exists():
        print(f"✗ 文件不存在: {path}")
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# API-based import
# ---------------------------------------------------------------------------

def import_docs_via_api(api_url: str, token: str, snapshot: dict, overwrite: bool) -> dict:
    """POST /v1/content/docs/import"""
    import urllib.request
    import urllib.error

    url = f"{api_url.rstrip('/')}/v1/content/docs/import?overwrite={'true' if overwrite else 'false'}"
    data = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"✗ 导入版本说明失败: HTTP {exc.code} — {exc.read().decode()}")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"✗ 无法连接后端: {exc.reason}")
        sys.exit(1)

    return body.get("data", body)


# ---------------------------------------------------------------------------
# DB-based import
# ---------------------------------------------------------------------------

def import_docs_via_db(database_url: str, snapshot: dict, overwrite: bool) -> dict:
    sys.path.insert(0, str(BACKEND_ROOT))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.content.content_blocks import import_docs_snapshot

    engine = create_engine(database_url)
    session = sessionmaker(bind=engine)()
    try:
        result = import_docs_snapshot(session, snapshot, overwrite=overwrite, default_updated_by="admin_import")
        return result
    finally:
        session.close()


def import_prompts_via_api(api_url: str, token: str, snapshot: dict, overwrite: bool) -> dict:
    """POST /v1/content/prompts/import"""
    import urllib.request
    import urllib.error

    url = f"{api_url.rstrip('/')}/v1/content/prompts/import?overwrite={'true' if overwrite else 'false'}"
    data = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"✗ 导入系统提示词失败: HTTP {exc.code} — {exc.read().decode()}")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"✗ 无法连接后端: {exc.reason}")
        sys.exit(1)

    return body.get("data", body)


def import_prompts_via_db(database_url: str, snapshot: dict, overwrite: bool) -> dict:
    sys.path.insert(0, str(BACKEND_ROOT))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.content.content_blocks import import_prompt_snapshot

    engine = create_engine(database_url)
    session = sessionmaker(bind=engine)()
    try:
        return import_prompt_snapshot(
            session, snapshot, overwrite=overwrite, default_updated_by="admin_import"
        )
    finally:
        session.close()


def import_catalog_overrides_via_db(database_url: str, overrides: list, overwrite: bool) -> dict:
    sys.path.insert(0, str(BACKEND_ROOT))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.db.models import CatalogOverride, Base

    engine = create_engine(database_url)
    Base.metadata.create_all(engine, tables=[CatalogOverride.__table__], checkfirst=True)
    session = sessionmaker(bind=engine)()

    imported, skipped = 0, 0
    try:
        for item in overrides:
            existing = (
                session.query(CatalogOverride)
                .filter_by(user_id=item["user_id"], kind=item["kind"], item_id=item["item_id"])
                .first()
            )
            if existing:
                if overwrite:
                    existing.enabled = item["enabled"]
                    existing.config_data = item.get("config_data", {})
                    imported += 1
                else:
                    skipped += 1
            else:
                session.add(CatalogOverride(
                    user_id=item["user_id"],
                    kind=item["kind"],
                    item_id=item["item_id"],
                    enabled=item["enabled"],
                    config_data=item.get("config_data", {}),
                ))
                imported += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return {"imported": imported, "skipped": skipped}


# ---------------------------------------------------------------------------
# Catalog.json file replacement
# ---------------------------------------------------------------------------

def import_catalog_json(catalog_data: dict, dry_run: bool) -> None:
    """Replace catalog.json with exported version, keeping a backup."""
    if not catalog_data:
        print("     catalog.json: 快照中无数据，跳过")
        return

    if dry_run:
        for kind in ("skills", "agents", "mcp", "kb"):
            items = catalog_data.get(kind, [])
            print(f"     [dry-run] catalog.json → {kind}: {len(items)} 项")
        return

    # Backup existing
    if CATALOG_JSON.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = CATALOG_JSON.with_suffix(f".backup_{ts}.json")
        shutil.copy2(CATALOG_JSON, backup)
        print(f"     已备份: {backup}")

    CATALOG_JSON.write_text(
        json.dumps(catalog_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for kind in ("skills", "agents", "mcp", "kb"):
        items = catalog_data.get(kind, [])
        print(f"     catalog.json → {kind}: {len(items)} 项")

    print("     ⚠ catalog.json 已更新，需要重启后端才能生效:")
    print("       docker-compose up -d --build backend")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="导入版本说明与能力中心数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--api-url", help="后端 API 地址，如 http://<HOST>/api")
    source.add_argument("--database-url", help="直接连接数据库")

    parser.add_argument("--admin-token", help="ADMIN_TOKEN（默认从 .env 读取）")
    parser.add_argument("--docs", type=Path, help="版本说明快照文件路径 (docs_snapshot_*.json)")
    parser.add_argument("--catalog", type=Path, help="能力中心快照文件路径 (catalog_snapshot_*.json)")
    parser.add_argument("--prompts", type=Path, help="系统提示词版本池快照文件路径 (prompts_snapshot_*.json)")
    parser.add_argument("--no-overwrite", action="store_true", help="不覆盖已有数据")
    parser.add_argument("--dry-run", action="store_true", help="试运行，仅输出将要执行的操作")

    args = parser.parse_args()

    if not args.docs and not args.catalog and not args.prompts:
        parser.error("请至少指定 --docs / --catalog / --prompts 之一")

    token = args.admin_token or _load_env_token()
    overwrite = not args.no_overwrite

    print("=" * 50)
    print("HugAgentOS 数据导入")
    print("=" * 50)
    if args.dry_run:
        print("⚠ 试运行模式 — 不会实际写入任何数据\n")

    # ── 1. Release notes ─────────────────────────────────────────────────
    if args.docs:
        print("[1/2] 导入版本说明 (content_blocks) ...")
        snapshot = _read_json(args.docs)

        # Stats
        blocks = snapshot.get("blocks", {})
        for name, block in blocks.items():
            count = len(block.get("payload", []))
            print(f"     {name}: {count} 条")

        if args.dry_run:
            print("     [dry-run] 跳过实际写入")
        elif args.api_url:
            if not token:
                print("✗ 需要 ADMIN_TOKEN，请通过 --admin-token 指定或在 .env 中设置")
                sys.exit(1)
            result = import_docs_via_api(args.api_url, token, snapshot, overwrite)
            print(f"     结果: imported={result.get('imported', [])}, skipped={result.get('skipped', [])}")
        else:
            result = import_docs_via_db(args.database_url, snapshot, overwrite)
            print(f"     结果: imported={result.get('imported', [])}, skipped={result.get('skipped', [])}")

    # ── 2. Capability center ─────────────────────────────────────────────
    if args.catalog:
        print("\n[2/2] 导入能力中心 (catalog) ...")
        catalog_data = _read_json(args.catalog)

        # 2a. catalog.json
        catalog_json_data = catalog_data.get("catalog_json")
        if catalog_json_data:
            print("  [a] catalog.json:")
            import_catalog_json(catalog_json_data, args.dry_run)

        # 2b. catalog_overrides
        overrides = catalog_data.get("catalog_overrides", [])
        if overrides:
            print(f"  [b] catalog_overrides: {len(overrides)} 条")
            if args.dry_run:
                print("     [dry-run] 跳过实际写入")
            elif args.database_url:
                result = import_catalog_overrides_via_db(args.database_url, overrides, overwrite)
                print(f"     结果: imported={result['imported']}, skipped={result['skipped']}")
            else:
                print("     ⚠ API 模式不支持批量导入 catalog_overrides，需要使用 --database-url")
        elif not catalog_json_data:
            print("     快照中无有效数据")

    # ── 3. System prompts + prompt hub ───────────────────────────────────
    if args.prompts:
        print("\n[+] 导入系统提示词 (prompt_versions) + 提示词广场 (prompt_hub) ...")
        snapshot = _read_json(args.prompts)

        # Stats
        blocks = snapshot.get("blocks", {})
        pv_payload = blocks.get("prompt_versions", {}).get("payload", {})
        versions = pv_payload.get("versions", []) if isinstance(pv_payload, dict) else []
        active = pv_payload.get("active", {}) if isinstance(pv_payload, dict) else {}
        by_kind: dict = {}
        for v in versions:
            by_kind[v.get("kind", "?")] = by_kind.get(v.get("kind", "?"), 0) + 1
        for kind, n in sorted(by_kind.items()):
            print(f"     prompt_versions/{kind}: {n} 个版本（激活: {active.get(kind)}）")
        hub_payload = blocks.get("prompt_hub", {}).get("payload", [])
        hub_count = len(hub_payload) if isinstance(hub_payload, list) else 0
        print(f"     prompt_hub: {hub_count} 条提示词模板")

        if args.dry_run:
            print("     [dry-run] 跳过实际写入")
        elif args.api_url:
            if not token:
                print("✗ 需要 ADMIN_TOKEN，请通过 --admin-token 指定或在 .env 中设置")
                sys.exit(1)
            result = import_prompts_via_api(args.api_url, token, snapshot, overwrite)
            print(f"     结果: imported={result.get('imported', [])}, skipped={result.get('skipped', [])}")
        else:
            result = import_prompts_via_db(args.database_url, snapshot, overwrite)
            print(f"     结果: imported={result.get('imported', [])}, skipped={result.get('skipped', [])}")
            print("     ⚠ 直连数据库导入：需重启后端，提示词缓存才会刷新")

    print("\n" + "=" * 50)
    if args.dry_run:
        print("✓ 试运行完成（未写入任何数据）")
    else:
        print("✓ 导入完成")
    print("=" * 50)


if __name__ == "__main__":
    main()
