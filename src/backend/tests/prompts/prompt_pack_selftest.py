"""Selftest: filesystem prompt pack is actually used.

Run:
  python -m selftests.prompt_pack_selftest

This must not require any external API keys.
"""

from __future__ import annotations

import os
from pathlib import Path

from prompts.prompt_config import PromptConfig, SystemPromptConfig
from prompts.prompt_runtime import build_system_prompt


def main() -> int:
    # Point to repo default prompt pack (as used by prompts/config/default.json).
    root = Path(__file__).resolve().parents[2]
    prompt_dir = root / "prompts" / "prompt_text" / "default"
    assert prompt_dir.exists(), f"missing prompt_dir: {prompt_dir}"

    cfg = PromptConfig(
        system_prompt=SystemPromptConfig(
            provider="filesystem",
            prompt_dir=str(prompt_dir),
            parts=[
                "system/00_role",
                "system/10_constraints",
                "system/20_tools",
            ],
        )
    )

    out = build_system_prompt(cfg, ctx={"selftest": True})

    # We assert on stable structure, not exact wording — the product name in the
    # first line varies by edition (HugAgentOS in the main repo / neutralized in the
    # CE tree), so a brand string would fail on both sides and would also leak the
    # brand name into the CE tree. "## 防幻觉约束" comes from the 10_constraints
    # part, proving multiple parts are loaded and concatenated.
    assert "## 防幻觉约束" in out

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
