"""Existing desktop instructions upgrade without changing custom or cloud text."""

from copy import deepcopy
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from core.db.models import ContentBlock
from core.db.desktop_delivery_repair import upgrade_desktop_delivery_prompts


def test_upgrade_stock_delivery_and_preserve_custom_content():
    legacy = "本机文件不会通过 /myspace 自动同步；需要下载链接时使用 sandbox_get_artifact。"
    original = {
        "active": {"desktop": "custom"},
        "versions": [
            {
                "kind": "desktop",
                "id": "default",
                "parts": [
                    {
                        "part_id": "bash_tool",
                        "content": "prefix\n" + legacy + "\nsuffix",
                        "is_enabled": False,
                    }
                ],
            },
            {
                "kind": "desktop",
                "id": "custom",
                "parts": [{"part_id": "bash_tool", "content": "Administrator instructions"}],
            },
            {
                "kind": "system",
                "id": "cloud",
                "parts": [{"part_id": "bash_tool", "content": legacy}],
            },
        ],
    }
    engine = create_engine("sqlite://")
    ContentBlock.__table__.create(engine)
    with Session(engine) as db:
        db.add(ContentBlock(id="prompt_versions", payload=deepcopy(original)))
        db.commit()
        assert upgrade_desktop_delivery_prompts(db.connection()) == 1
        db.commit()
        db.expire_all()
        result = db.get(ContentBlock, "prompt_versions").payload
        content = result["versions"][0]["parts"][0]["content"]
        assert "pin_to_workspace(file_paths=" in content
        assert "sandbox_get_artifact" not in content
        assert content.startswith("prefix\n") and content.endswith("\nsuffix")
        assert result["versions"][0]["parts"][0]["is_enabled"] is False
        assert result["versions"][1:] == original["versions"][1:]
        assert result["active"] == original["active"]
        assert upgrade_desktop_delivery_prompts(db.connection()) == 0
    engine.dispose()
