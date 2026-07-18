"""Minimal selftest: config load + prompt render.

Run:
  python -m selftests.prompt_config_selftest

This test must not require any external API keys.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from prompts.prompt_config import PromptConfig, SystemPromptConfig, load_prompt_config
from prompts.prompt_runtime import build_system_prompt


# Anchor on a **structural marker** rather than brand wording: the product name in
# the prompt's first line varies by build (main repo HugAgentOS / neutralized in the CE
# derived tree), so a brand word would mismatch on both sides and leak the brand into
# the CE tree. "## 防幻觉约束" comes from the 10_constraints part, proving multiple
# parts are loaded and concatenated, and stays constant across brands.
_ANCHOR = "## 防幻觉约束"


def test_filesystem_prompt_pack_is_used() -> None:
    # Should load from the repo's default prompt pack by config.
    cfg = PromptConfig(
        system_prompt=SystemPromptConfig(
            provider="filesystem",
            prompt_dir="./prompts/prompt_text/default",
            parts=[
                "system/00_role",
                "system/10_constraints",
                "system/20_tools",
            ],
        )
    )
    out = build_system_prompt(cfg, ctx={"selftest": True})
    assert _ANCHOR in out, "filesystem provider should include prompt pack content"


def test_filesystem_missing_files_fallbacks_to_minimal() -> None:
    with tempfile.TemporaryDirectory() as td:
        os.environ["PROMPT_DIR"] = str(Path(td))
        cfg = PromptConfig(system_prompt=SystemPromptConfig(provider="filesystem", prompt_dir=str(Path(td)), parts=["system/00_time_role"]))
        out = build_system_prompt(cfg, ctx={"selftest": True})
        assert out.strip(), "fallback prompt must be non-empty"


def main() -> int:
    cfg = load_prompt_config()
    prompt = build_system_prompt(cfg, ctx={"selftest": True})

    assert cfg.version >= 1
    assert isinstance(prompt, str)
    assert prompt.strip(), "system prompt must be non-empty"

    # Guardrails for prompt provider routing.
    test_filesystem_prompt_pack_is_used()
    test_filesystem_missing_files_fallbacks_to_minimal()

    print("OK: prompt config loaded; system prompt provider routing works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
