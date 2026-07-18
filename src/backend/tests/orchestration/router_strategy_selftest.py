"""Selftest: router strategy selection.

Run:
  python -m selftests.router_strategy_selftest
"""

from __future__ import annotations

import os


def main() -> int:
    # Default: main_only
    os.environ.pop("ROUTER_STRATEGY", None)
    from orchestration.strategy import get_router_strategy

    s = get_router_strategy()
    assert s.route("生成一份报告") == "main"

    # rule_based is no longer supported; falls back to main
    os.environ["ROUTER_STRATEGY"] = "rule_based"
    s = get_router_strategy()
    assert s.route("请生成报告并导出docx") == "main"

    # llm_router placeholder
    os.environ["ROUTER_STRATEGY"] = "llm_router"
    s = get_router_strategy()
    assert s.route("report please") == "main"

    print("router_strategy_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
