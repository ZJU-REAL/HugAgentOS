"""我的空间文件列表的分页：超过一页的文件必须能整批取到，不被任何固定条数截断。"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from core.db.models import Artifact, UserShadow
from core.db.repository import ArtifactRepository

TOTAL_FILES = 250
USER_ID = "u-paging"


@pytest.fixture
def user_with_files(db_session):
    db_session.add(UserShadow(user_id=USER_ID, username="paging"))
    base = datetime(2026, 1, 1)
    for i in range(TOTAL_FILES):
        db_session.add(
            Artifact(
                artifact_id=f"art-{i:04d}",
                user_id=USER_ID,
                type="document",
                title=f"file-{i:04d}",
                filename=f"file-{i:04d}.txt",
                size_bytes=10,
                mime_type="text/plain",
                storage_key=f"key/{i}",
                created_at=base + timedelta(minutes=i),
            )
        )
    db_session.commit()
    return db_session


def test_repository_pages_through_every_file(user_with_files):
    repo = ArtifactRepository(user_with_files)
    seen: list[str] = []
    page = 1
    while True:
        rows, total = repo.list_by_user_with_chat(
            user_id=USER_ID, page=page, page_size=100, folder_id="__root__"
        )
        assert total == TOTAL_FILES
        if not rows:
            break
        seen.extend(row["artifact"].artifact_id for row in rows)
        page += 1
    assert len(seen) == TOTAL_FILES
    assert len(set(seen)) == TOTAL_FILES


def test_repository_unlimited_page_size_returns_everything(user_with_files):
    repo = ArtifactRepository(user_with_files)
    rows, total = repo.list_by_user_with_chat(
        user_id=USER_ID, page_size=None, folder_id="__root__"
    )
    assert total == TOTAL_FILES
    assert len(rows) == TOTAL_FILES


class _StubToolkit:
    def __init__(self):
        self.functions = {}

    def register_tool_function(self, fn, **_kwargs):
        self.functions[fn.__name__] = fn


@pytest.fixture
def list_tool(monkeypatch, user_with_files):
    from sqlalchemy.orm import sessionmaker

    from core.db import engine as engine_module
    from core.llm.tools.myspace_tool import register_myspace_tools

    monkeypatch.setattr(
        engine_module, "SessionLocal", sessionmaker(bind=user_with_files.get_bind())
    )
    toolkit = _StubToolkit()
    register_myspace_tools(toolkit, user_id=USER_ID)
    return toolkit.functions["space_list_myspace_files"]


def _call(tool, **kwargs) -> dict:
    block = asyncio.run(tool(**kwargs)).content[0]
    text = block["text"] if isinstance(block, dict) else block.text
    return json.loads(text)


def test_tool_reports_more_pages_and_serves_them(list_tool):
    first = _call(list_tool, limit=100, page=1)
    assert first["total"] == TOTAL_FILES
    assert len(first["items"]) == 100
    assert first["has_more"] is True
    assert first["total_pages"] == 3

    names: set[str] = {item["name"] for item in first["items"]}
    for page in (2, 3):
        payload = _call(list_tool, limit=100, page=page)
        names.update(item["name"] for item in payload["items"])
    assert len(names) == TOTAL_FILES
    assert _call(list_tool, limit=100, page=3)["has_more"] is False


def test_tool_page_size_is_not_capped(list_tool):
    payload = _call(list_tool, limit=200, page=1)
    assert len(payload["items"]) == 200
    assert payload["page_size"] == 200


def test_tool_zero_limit_returns_every_file(list_tool):
    payload = _call(list_tool, limit=0)
    assert len(payload["items"]) == TOTAL_FILES
    assert payload["page_size"] is None
    assert payload["has_more"] is False
