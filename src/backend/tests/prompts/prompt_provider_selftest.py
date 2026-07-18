"""Selftests for PromptProvider.

Run:
  python -m selftests.prompt_provider_selftest

This test must not require any external API keys.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from prompts.provider import FilesystemPromptProvider, InlinePromptProvider


def _assert_raises(fn, exc_type: type[BaseException]) -> None:
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def test_strict_vars() -> None:
    p = InlinePromptProvider(template="hello {name}", strict_vars=True)
    _assert_raises(lambda: p.get_prompt("system", "system", vars={}), KeyError)


def test_loose_vars() -> None:
    p = InlinePromptProvider(template="hello {name}", strict_vars=False)
    out = p.get_prompt("system", "system", vars={})
    assert out == "hello {name}"


def test_filesystem_missing_file_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        p = FilesystemPromptProvider(prompt_dir=d, strict_vars=True)
        out = p.get_prompt("system", "system", vars={})
        assert isinstance(out, str) and out.strip(), "must fallback to non-empty hardcoded system prompt"


def main() -> int:
    # Ensure env does not affect these unit-style tests.
    os.environ.pop("PROMPT_DIR", None)
    os.environ.pop("PROMPT_PROVIDER", None)
    os.environ.pop("PROMPT_STRICT_VARS", None)

    test_strict_vars()
    test_loose_vars()
    test_filesystem_missing_file_fallback()

    print("OK: prompt provider strict/loose + filesystem fallback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
