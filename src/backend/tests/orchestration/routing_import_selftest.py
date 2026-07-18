"""Selftest: routing modules are import-safe and strategy is callable.

Run:
  python -m selftests.routing_import_selftest
"""

from __future__ import annotations


def main() -> int:
    from orchestration.strategy import get_router_strategy

    s = get_router_strategy()
    out = s.route("hello", context={"chat_id": "x"})
    assert out == "main", f"expected main routing, got {out!r}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
