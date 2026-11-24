"""PromptConfig loading and the filesystem system-prompt provider path."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import engine as db_engine
from core.db.models import AdminPromptPart, ContentBlock
from core.llm import prompt_storage as pvs
from prompts import prompt_runtime
from prompts import project_section
from prompts.prompt_config import PromptConfig, SystemPromptConfig, load_prompt_config
from prompts.prompt_runtime import build_system_prompt

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACK = BACKEND_ROOT / "prompts" / "prompt_text" / "default"
PACK_PARTS = ["system/00_role", "system/10_constraints", "system/20_tools"]


@pytest.fixture(autouse=True)
def isolated_prompt_sources(monkeypatch):
    """No active prompt version and no DB parts, so the filesystem pack is used."""
    engine = create_engine("sqlite://")
    ContentBlock.__table__.create(engine)
    AdminPromptPart.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(pvs, "SessionLocal", sessions)
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    monkeypatch.setattr(prompt_runtime, "_load_db_prompt_parts", lambda: {})
    monkeypatch.setattr(prompt_runtime, "_get_db_prompt_version", lambda: "test")
    monkeypatch.setattr(project_section, "_load_db_prompt_parts", lambda: {})
    monkeypatch.delenv("PROMPT_DIR", raising=False)
    prompt_runtime.invalidate_prompt_cache()
    yield
    prompt_runtime.invalidate_prompt_cache()
    engine.dispose()


def _first_heading(part_id: str) -> str:
    text = (DEFAULT_PACK / f"{part_id}.system.md").read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith("## "))


def test_default_config_loads_and_renders_non_empty_prompt():
    cfg = load_prompt_config()
    assert cfg.version >= 1

    prompt = build_system_prompt(cfg, ctx={"now": "2026-01-01"})
    assert isinstance(prompt, str)
    assert prompt.strip()


def test_filesystem_provider_concatenates_repo_prompt_pack_parts():
    # Anchor on each part's own section heading rather than brand wording, so the
    # check survives both prompt rewrites and the CE brand transform.
    cfg = PromptConfig(
        system_prompt=SystemPromptConfig(
            provider="filesystem",
            prompt_dir="./prompts/prompt_text/default",
            parts=PACK_PARTS,
        )
    )
    out = build_system_prompt(cfg, ctx={"now": "2026-01-01"})

    headings = [_first_heading(p) for p in PACK_PARTS]
    positions = [out.index(h) for h in headings]
    assert positions == sorted(positions), "parts must be concatenated in config order"


def test_filesystem_provider_renders_parts_from_custom_dir(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "a.system.md").write_text("PART-A", encoding="utf-8")
    (tmp_path / "system" / "b.system.md").write_text("PART-B on {now}", encoding="utf-8")
    cfg = PromptConfig(
        system_prompt=SystemPromptConfig(
            provider="filesystem", prompt_dir=str(tmp_path), parts=["system/a", "system/b"]
        )
    )

    out = build_system_prompt(cfg, ctx={"now": "2026-01-01"})

    assert "PART-A" in out
    assert "PART-B on 2026-01-01" in out
    assert out.index("PART-A") < out.index("PART-B")


def test_filesystem_missing_files_fall_back_to_minimal_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMPT_DIR", str(tmp_path))
    cfg = PromptConfig(
        system_prompt=SystemPromptConfig(
            provider="filesystem", prompt_dir=str(tmp_path), parts=["system/00_time_role"]
        )
    )
    out = build_system_prompt(cfg, ctx={"now": "2026-01-01"})
    assert out.strip(), "fallback prompt must be non-empty"
