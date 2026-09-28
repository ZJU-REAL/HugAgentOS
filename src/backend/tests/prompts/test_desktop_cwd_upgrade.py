"""Saved desktop defaults follow cwd without replacing administrator content."""

from copy import deepcopy
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from core.db.models import ContentBlock
from core.db.desktop_delivery_repair import upgrade_desktop_cwd_prompts, CWD_REPLACEMENTS


def test_saved_desktop_cwd_upgrade_preserves_custom_cloud_and_active_version():
    legacy = "project_root 是绑定的项目目录，不代表工具已经切换 cwd。操作项目文件使用真实绝对路径；执行项目命令时在同一次 Bash 调用中先 cd 到带引号的项目路径。"
    original = {
        "active": {"desktop": "custom"},
        "versions": [
            {
                "kind": "desktop",
                "id": "stock",
                "parts": [
                    {
                        "part_id": "guidance",
                        "content": "prefix\n" + legacy + "\nsuffix",
                        "is_enabled": False,
                    },
                    {
                        "part_id": "project",
                        "content": "项目文件使用真实绝对路径操作，不需要上传到「我的空间」。",
                    },
                ],
            },
            {
                "kind": "desktop",
                "id": "custom",
                "parts": [{"part_id": "guidance", "content": "Custom administrator rules"}],
            },
            {
                "kind": "system",
                "id": "cloud",
                "parts": [{"part_id": "guidance", "content": legacy}],
            },
        ],
    }
    engine = create_engine("sqlite://")
    ContentBlock.__table__.create(engine)
    with Session(engine) as db:
        db.add(ContentBlock(id="prompt_versions", payload=deepcopy(original)))
        db.commit()
        assert upgrade_desktop_cwd_prompts(db.connection()) == 2
        db.commit()
        db.expire_all()
        actual = db.get(ContentBlock, "prompt_versions").payload
        stock = actual["versions"][0]["parts"]
        assert "cwd 与 project_root 相同" in stock[0]["content"]
        assert stock[0]["content"].startswith("prefix\n") and stock[0]["content"].endswith(
            "\nsuffix"
        )
        assert stock[0]["is_enabled"] is False
        assert "相对路径基准" in stock[1]["content"]
        assert actual["versions"][1:] == original["versions"][1:]
        assert actual["active"] == original["active"]
        assert upgrade_desktop_cwd_prompts(db.connection()) == 0
    engine.dispose()
    template_root = Path(__file__).resolve().parents[2] / "prompts/prompt_text/desktop"
    for part, (_, replacement) in CWD_REPLACEMENTS.items():
        assert replacement in (template_root / f"{part}.system.md").read_text()


def test_alembic_revision_runs_transactionally_and_can_restore_snapshot(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration_file = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/localcwd01_project_execution_directory.py"
    )
    spec = importlib.util.spec_from_file_location("cwd_migration", migration_file)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    original = {
        "versions": [
            {
                "kind": "desktop",
                "parts": [
                    {
                        "part_id": "project",
                        "content": "项目文件使用真实绝对路径操作，不需要上传到「我的空间」。",
                    }
                ],
            }
        ]
    }
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    ContentBlock.__table__.create(engine)
    with Session(engine) as db:
        db.add(ContentBlock(id="prompt_versions", payload=deepcopy(original)))
        db.commit()
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.upgrade()
        db.expire_all()
        assert "相对路径基准" in str(db.get(ContentBlock, "prompt_versions").payload)
        db.rollback()
        assert db.get(ContentBlock, "prompt_versions").payload == original
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.upgrade()
        db.commit()
        # Post-commit rollback follows the documented pre-upgrade snapshot restore.
        db.get(ContentBlock, "prompt_versions").payload = deepcopy(original)
        db.commit()
        db.expire_all()
        assert db.get(ContentBlock, "prompt_versions").payload == original
    engine.dispose()
