"""PromptProvider variable handling and filesystem fallback."""

from __future__ import annotations

import pytest

from prompts.provider import FilesystemPromptProvider, InlinePromptProvider


@pytest.fixture(autouse=True)
def _clean_prompt_env(monkeypatch):
    for name in ("PROMPT_DIR", "PROMPT_PROVIDER", "PROMPT_STRICT_VARS"):
        monkeypatch.delenv(name, raising=False)


def test_strict_vars_raises_on_missing_variable():
    p = InlinePromptProvider(template="hello {name}", strict_vars=True)
    with pytest.raises(KeyError):
        p.get_prompt("system", "system", vars={})


def test_loose_vars_keeps_missing_placeholder():
    p = InlinePromptProvider(template="hello {name}", strict_vars=False)
    assert p.get_prompt("system", "system", vars={}) == "hello {name}"


def test_filesystem_missing_file_falls_back_to_minimal_system_prompt(tmp_path):
    p = FilesystemPromptProvider(prompt_dir=tmp_path, strict_vars=True)
    out = p.get_prompt("system", "system", vars={})
    assert isinstance(out, str) and out.strip()


def test_filesystem_reads_role_specific_file(tmp_path):
    (tmp_path / "greet.system.md").write_text("hi {who}", encoding="utf-8")
    p = FilesystemPromptProvider(prompt_dir=tmp_path, strict_vars=True)
    assert p.get_prompt("greet", "system", vars={"who": "there"}) == "hi there"
    # Non-system prompt ids with no file render empty rather than the system fallback.
    assert p.get_prompt("missing", "system", vars={}) == ""
