"""Saved prompts must be visible to already-warm backend workers."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import engine as db_engine
from core.db.models import AdminPromptPart, ContentBlock, SystemConfig
from core.services import prompt_version_service as pvs


@pytest.fixture
def prompt_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'prompts.db'}"
    engine = create_engine(url)
    ContentBlock.__table__.create(engine)
    AdminPromptPart.__table__.create(engine)
    SystemConfig.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(pvs, "SessionLocal", sessions)
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    from prompts import prompt_runtime
    from core.services import system_config

    monkeypatch.setattr(system_config, "SessionLocal", sessions)
    monkeypatch.setattr(system_config.SystemConfigService, "_instance", None)
    prompt_runtime.invalidate_prompt_cache()
    yield url, sessions
    prompt_runtime.invalidate_prompt_cache()
    engine.dispose()


def save_in_other_worker(url, kind, version_id, content):
    script = """
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.db import engine as db_engine
from core.services import prompt_version_service as pvs
sessions = sessionmaker(bind=create_engine(sys.argv[1]))
pvs.SessionLocal = sessions
db_engine.SessionLocal = sessions
from api.routes.v1.prompt_management import update_pool_version, VersionUpsertRequest, PromptPartPayload
with sessions() as db:
    update_pool_version(
        sys.argv[2], sys.argv[3],
        VersionUpsertRequest(kind=sys.argv[2], parts=[
            PromptPartPayload(part_id="guidance" if sys.argv[2] == "desktop" else "system/00_role",
                              content=sys.argv[4])]),
        db=db,
    )
"""
    result = subprocess.run(
        [sys.executable, "-c", script, url, kind, version_id, content],
        capture_output=True,
        text=True,
        timeout=45,
        env={**os.environ, "DATABASE_URL": url},
    )
    assert result.returncode == 0, result.stderr


def test_saved_version_is_visible_to_another_warm_worker(prompt_db):
    from api.routes.v1.prompt_management import get_pool_version

    url, sessions = prompt_db
    pvs.upsert_version(
        "system", "default", parts=[{"part_id": "system/00_role", "content": "BEFORE_SAVE"}]
    )
    with sessions() as db:
        assert (
            get_pool_version("system", "default", db=db)["data"]["parts"][0]["content"]
            == "BEFORE_SAVE"
        )

    save_in_other_worker(url, "system", "default", "AFTER_SAVE")

    with sessions() as db:
        assert (
            get_pool_version("system", "default", db=db)["data"]["parts"][0]["content"]
            == "AFTER_SAVE"
        )


@pytest.mark.asyncio
async def test_preview_and_runtime_use_saved_prompts_without_local_invalidation(
    prompt_db, monkeypatch
):
    from api.routes.v1.prompt_management import desktop_preview, DesktopPreviewRequest
    from core.config import local_mode
    from prompts.prompt_config import PromptConfig
    from prompts.prompt_runtime import build_system_prompt

    url, sessions = prompt_db
    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: False)
    pvs.seed_from_filesystem()
    pvs.upsert_version(
        "system", "default", parts=[{"part_id": "system/00_role", "content": "SYSTEM_BEFORE"}]
    )
    pvs.upsert_version(
        "desktop", "default", parts=[{"part_id": "guidance", "content": "DESKTOP_BEFORE"}]
    )
    config = PromptConfig()
    context = {"chat_id": "freshness-test"}
    assert "SYSTEM_BEFORE" in build_system_prompt(config, context)
    with sessions() as db:
        before = (await desktop_preview(DesktopPreviewRequest(version_id="default"), db=db))[
            "data"
        ]["prompt"]
    assert "SYSTEM_BEFORE" in before
    assert "DESKTOP_BEFORE" in before

    save_in_other_worker(url, "system", "default", "SYSTEM_AFTER")
    save_in_other_worker(url, "desktop", "default", "DESKTOP_AFTER")

    runtime = build_system_prompt(config, context)
    assert "SYSTEM_AFTER" in runtime
    assert "SYSTEM_BEFORE" not in runtime
    with sessions() as db:
        after = (await desktop_preview(DesktopPreviewRequest(version_id="default"), db=db))["data"][
            "prompt"
        ]
    assert "SYSTEM_AFTER" in after
    assert "DESKTOP_AFTER" in after
    assert "SYSTEM_BEFORE" not in after
    assert "DESKTOP_BEFORE" not in after


def test_warm_worker_does_not_overwrite_other_workers_saved_version(prompt_db):
    url, _ = prompt_db
    pvs.upsert_version(
        "system", "default", parts=[{"part_id": "system/00_role", "content": "FIRST_BEFORE"}]
    )
    pvs.upsert_version(
        "code_exec", "default", parts=[{"part_id": "system/00_role", "content": "SECOND_BEFORE"}]
    )
    assert pvs.get_version("system", "default")["parts"][0]["content"] == "FIRST_BEFORE"
    save_in_other_worker(url, "system", "default", "FIRST_AFTER")

    pvs.upsert_version(
        "code_exec", "default", parts=[{"part_id": "system/00_role", "content": "SECOND_AFTER"}]
    )
    assert pvs.get_version("system", "default")["parts"][0]["content"] == "FIRST_AFTER"
    assert pvs.get_version("code_exec", "default")["parts"][0]["content"] == "SECOND_AFTER"
