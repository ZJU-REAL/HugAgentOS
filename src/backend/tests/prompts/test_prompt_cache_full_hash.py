"""Prompt cache keys must cover complete dynamic prompt inputs."""

from __future__ import annotations

from prompts.prompt_config import PromptConfig, SystemPromptConfig
from prompts import prompt_runtime
from prompts import project_section
from core.llm.execution_manifest import PromptManifestBuilder


def test_project_instructions_after_character_200_do_not_share_cache(monkeypatch):
    monkeypatch.setattr(prompt_runtime, "_load_db_prompt_parts", lambda: {})
    monkeypatch.setattr(prompt_runtime, "_get_db_prompt_version", lambda: "test")
    monkeypatch.setattr(project_section, "_load_db_prompt_parts", lambda: {})
    prompt_runtime.invalidate_prompt_cache()

    config = PromptConfig(
        system_prompt=SystemPromptConfig(provider="inline", inline_template="BASE")
    )
    prefix = "x" * 200
    common = {
        "project_id": "project-1",
        "project_name": "private project",
        "project_folder_name": "folder",
        "project_folder_kind": "personal",
        "project_files": [],
        "now": "2026-08-24",
    }

    first = prompt_runtime.build_system_prompt(
        config, ctx={**common, "project_instructions": prefix + "A"}
    )
    second = prompt_runtime.build_system_prompt(
        config, ctx={**common, "project_instructions": prefix + "B"}
    )

    assert first.endswith("A")
    assert second.endswith("B")
    assert first != second


def test_cache_hit_replays_identical_section_manifest(monkeypatch):
    monkeypatch.setattr(prompt_runtime, "_load_db_prompt_parts", lambda: {})
    monkeypatch.setattr(prompt_runtime, "_get_db_prompt_version", lambda: "test")
    monkeypatch.setattr(project_section, "_load_db_prompt_parts", lambda: {})
    prompt_runtime.invalidate_prompt_cache()

    config = PromptConfig(
        system_prompt=SystemPromptConfig(provider="inline", inline_template="BASE {now}")
    )
    context = {
        "project_id": "project-1",
        "project_name": "private project",
        "project_instructions": "complete instructions",
        "project_folder_name": "folder",
        "project_folder_kind": "personal",
        "project_files": [{"name": "private.txt", "size_bytes": 20}],
        "now": "2026-08-24",
    }
    first_builder = PromptManifestBuilder(context=context)
    first = prompt_runtime.build_system_prompt(config, ctx=context, manifest_builder=first_builder)
    first_manifest = first_builder.build(final_prompt=first)

    cached_builder = PromptManifestBuilder(context=dict(reversed(list(context.items()))))
    cached = prompt_runtime.build_system_prompt(
        config, ctx=context, manifest_builder=cached_builder
    )
    cached_manifest = cached_builder.build(final_prompt=cached)

    assert first == cached
    assert first_manifest.prompt_manifest_hash == cached_manifest.prompt_manifest_hash
    assert first_manifest.aggregate_hash == cached_manifest.aggregate_hash


def test_literal_date_in_project_instructions_is_not_rewritten_on_cache_hit(monkeypatch):
    monkeypatch.setattr(prompt_runtime, "_load_db_prompt_parts", lambda: {})
    monkeypatch.setattr(prompt_runtime, "_get_db_prompt_version", lambda: "test")
    monkeypatch.setattr(project_section, "_load_db_prompt_parts", lambda: {})
    prompt_runtime.invalidate_prompt_cache()

    config = PromptConfig(
        system_prompt=SystemPromptConfig(provider="inline", inline_template="TODAY={now}")
    )
    common = {
        "project_id": "project-1",
        "project_name": "dated project",
        "project_instructions": "The fixed deadline is 2026-08-24; literal {now} stays.",
        "project_folder_name": "folder",
        "project_folder_kind": "personal",
        "project_files": [],
    }

    first = prompt_runtime.build_system_prompt(config, ctx={**common, "now": "2026-08-24"})
    next_day = prompt_runtime.build_system_prompt(config, ctx={**common, "now": "2026-08-25"})

    assert "TODAY=2026-08-25" in next_day
    assert "fixed deadline is 2026-08-24" in first
    assert "fixed deadline is 2026-08-24" in next_day
    assert "literal {now} stays" in next_day
