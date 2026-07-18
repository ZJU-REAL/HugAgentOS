"""Selftest: project-local catalog loader.

Run:
  python -m selftests.catalog_selftest
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def main() -> int:
    import core.config.catalog as cat

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "catalog.json"

        # Monkeypatch path to avoid touching repo config.
        cat._CATALOG_PATH = tmp  # type: ignore[attr-defined]

        data = cat.ensure_default_catalog()
        assert isinstance(data.get("skills"), list)
        assert isinstance(data.get("agents"), list)
        assert isinstance(data.get("mcp"), list)
        assert isinstance(data.get("kb"), list)

        # File should exist and be valid JSON.
        raw = tmp.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

        cat.set_enabled("mcp", "internet_search", False)
        assert cat.is_enabled("mcp", "internet_search") is False

        # Legacy schema migration should keep frontend shape and ignore router.
        legacy = {
            "router_strategy": {"enabled": True, "value": "rule_based"},
            "mcp_server": {"internet_search": {"enabled": False}},
        }
        tmp.write_text(json.dumps(legacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        migrated = cat.get_catalog()
        assert set(migrated.keys()) >= {"skills", "agents", "mcp", "kb"}
        assert "router_strategy" not in migrated
        assert cat.is_enabled("mcp", "internet_search") is False

    print("catalog_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
